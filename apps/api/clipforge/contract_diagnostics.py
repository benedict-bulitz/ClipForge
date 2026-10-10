"""Internal contract failure details; never a source of user-facing copy."""
import json
import re

from pydantic import ValidationError


def sanitized(value, secrets=()):
    if isinstance(value, dict):
        return {sanitized(str(key), secrets): "[REDACTED]" if re.search(r"authorization|api[_-]?key|authentication", str(key), re.IGNORECASE)
                else sanitized(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitized(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", value)
        value = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", value)
        return re.sub(r"(?i)((?:api[_-]?key|authorization|authentication)[\"']?\s*[:=]\s*)[^\n,;]+",
                      r"\1[REDACTED]", value)
    return value


class ContractGenerationFailure(ValueError):
    def __init__(self, message, *, stage, code, violations, settings, flags=None, explanations=None, extra=None):
        secrets = (settings.openai_api_key,)
        super().__init__(sanitized(message, secrets))
        kinds = {item["kind"] for item in violations}
        self.diagnostics = sanitized({
            "failure_stage": stage, "failure_code": code,
            "failure_kind": next(iter(kinds)) if len(kinds) == 1 else "mixed",
            "failed_invariants": violations, "verification_flags": flags or {},
            "reviewer_explanations": explanations or {}, **(extra or {}),
        }, secrets)


def violation(code, invariant, component_ids=(), kind="structural"):
    return {"code": code, "invariant": invariant, "component_ids": list(component_ids), "kind": kind}


def parse_contract_output(client, settings, stage, schema, messages):
    """Observe the existing parse call without changing parameters or retry policy."""
    try:
        response = client.beta.chat.completions.parse(
            model=settings.openai_director_model, messages=messages, response_format=schema,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic wrapper, still fail closed
        structural = isinstance(exc, (ValidationError, json.JSONDecodeError))
        code = "MALFORMED_STRUCTURED_OUTPUT" if structural else "PROVIDER_CALL_FAILED"
        raise ContractGenerationFailure(
            str(exc), stage=stage, code=code, settings=settings,
            violations=[violation(code, "The stage must return valid structured output.",
                                  kind="structural" if structural else "provider")],
            extra={"exception_type": type(exc).__name__, "provider_message": str(exc)},
        ) from None
    try:
        message = response.choices[0].message
        parsed = message.parsed
        refusal = getattr(message, "refusal", None)
    except (AttributeError, IndexError, TypeError):
        parsed, refusal = None, None
    if refusal or not isinstance(parsed, schema):
        code = "REVIEWER_REFUSED" if refusal else "INVALID_STRUCTURED_OUTPUT"
        raise ContractGenerationFailure(
            "Contract stage returned no valid structured output", stage=stage, code=code, settings=settings,
            violations=[violation(code, "Parsed output must match the requested schema and contain no refusal.")],
            explanations={"refusal": refusal} if refusal else {},
        )
    return parsed
