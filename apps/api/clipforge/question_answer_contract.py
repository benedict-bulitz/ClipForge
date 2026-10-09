import logging
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, Field, PrivateAttr, model_validator

from clipforge.config import Settings
from clipforge.novelty import fact_is_supported

logger = logging.getLogger("clipforge.contract")

class AnswerObligation(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    description: str = Field(description="What exactly must be answered or explained")
    is_primary: bool = Field(description="True if this is the core primary answer")
    is_required: bool = Field(description="True if the video must fail without this")
    @model_validator(mode="after")
    def primary_is_required(self):
        self.is_required = self.is_primary or self.is_required
        return self

    fact_ids: list[str] = Field(default_factory=list, description="Fact IDs from research that support this")

class CausalChainStep(BaseModel):
    id: str
    description: str
    depth_level: int

class QuestionAnswerContract(BaseModel):
    _minimality_review: dict = PrivateAttr(default_factory=dict)
    @model_validator(mode="after")
    def primary_slot_is_required(self):
        self.primary_answer_obligation.is_primary = True
        self.primary_answer_obligation.is_required = True
        return self

    question_type: Literal[
        "causal", "mechanistic", "historical_motive", "definition",
        "comparison", "quantity", "timeline", "consequence", "explanation", "mixed"
    ]
    core_question: str
    primary_answer_obligation: AnswerObligation
    required_supporting_obligations: list[AnswerObligation]
    optional_context: list[str]
    causal_mechanistic_chain: list[CausalChainStep] = Field(default_factory=list)
    historical_motive: str | None = None
    required_mechanism_concepts: list[str] = Field(default_factory=list)
    minimum_answer_depth: int = 1
    unresolved_research_gaps: list[str] = Field(default_factory=list)

def generate_contract(question: str, language: str, settings: Settings) -> QuestionAnswerContract:
    client = OpenAI(api_key=settings.openai_api_key)
    system_prompt = (
        "You are the ClipForge Contract Planner. "
        "Your task is to define the STRICT MINIMUM semantic answer obligations for a given question. "
        "A video script will be rejected if it does not satisfy the 'primary_answer_obligation' and all 'required_supporting_obligations'. "
        "Secondary facts must NEVER compensate for a missing primary answer.\n\n"
        "Guidelines:\n"
        "- For historical 'why' questions: The immediate purpose/motive is MANDATORY. Broad political context alone is insufficient. Specify 'historical_motive' as the type.\n"
        "- For scientific 'why' questions: Focus on the direct cause/mechanism. Avoid circular answers (restating the observed property instead of explaining its cause). Set 'minimum_answer_depth' to the level that genuinely resolves the curiosity.\n"
        "- Write obligation descriptions in the requested target language.\n"
        "- Do not require maximum depth if a shorter explanation resolves the question.\n"
        "- Stop at the first non-circular causal mechanism that explains the actual phenomenon. "
        "Ordinary short knowledge questions generally need the shortest causal chain that resolves them.\n"
        "- Do not require a generic underlying physics or perception layer when the direct material/process cause "
        "already resolves the question. For example, if process Y forms a reddish compound X that is widespread, "
        "that can resolve an appearance question: photon-level reflection/scattering is optional unless the user "
        "specifically asks why the material/color itself has that optical property.\n"
        "- Keep the primary obligation atomic enough to verify. Each required supporting obligation must be "
        "independently necessary to resolve the user's question, not merely nice deeper detail. "
        "Use optional_context for useful extensions beyond the minimum answer.\n"
        "- minimum_answer_depth represents necessary causal depth, not maximum possible depth. "
        "Do not impose a universal depth cap: genuinely multi-step questions still require all necessary steps.\n"
        "- Do not blindly treat every question as causal. Only require a causal chain if the question warrants it.\n"
    )
    response = client.beta.chat.completions.parse(
        model=settings.openai_director_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Question: {question}\nLanguage: {language}"},
        ],
        response_format=QuestionAnswerContract,
    )
    parsed = response.choices[0].message.parsed
    if not isinstance(parsed, QuestionAnswerContract):
        raise TypeError("Contract planner returned no valid contract")
    from clipforge.contract_minimality import guard_contract

    return guard_contract(question, language, parsed, client, settings)

class ObligationCoverage(BaseModel):
    obligation_id: str
    status: Literal["satisfied", "partially_satisfied", "missing", "circular", "unsupported", "insufficient_depth"]
    supporting_fact_ids: list[str] = Field(default_factory=list)
    reasoning: str

class ResearchCoverageReport(BaseModel):
    is_sufficient: bool = Field(description="True if all required obligations are satisfied")
    missing_obligations: list[str] = Field(default_factory=list, description="IDs of required obligations that are missing")
    coverage: list[ObligationCoverage]
    diagnostic_reason: str | None = None

def evaluate_research_coverage(contract: QuestionAnswerContract, facts: list[dict], settings: Settings) -> ResearchCoverageReport:
    client = OpenAI(api_key=settings.openai_api_key)
    system_prompt = (
        "You are the ClipForge Contract Verifier. "
        "Your task is to map provided research facts to the required answer obligations of a contract. "
        "A REQUIRED obligation must be fully satisfied by the provided facts. "
        "If a required obligation is missing, unsupported, or circular, you must mark it as such.\n\n"
        "Guidelines:\n"
        "- Do not allow the script to invent an answer if it's missing from the research.\n"
        "- Set 'is_sufficient' to true only if ALL REQUIRED obligations are satisfied.\n"
    )
    
    facts = [fact for fact in facts if fact_is_supported(fact)]
    facts_text = "\n".join([f"Fact ID {f.get('id')}: {f.get('claim')}" for f in facts])
    
    response = client.beta.chat.completions.parse(
        model=settings.openai_director_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Contract:\n{contract.model_dump_json(indent=2)}\n\nFacts:\n{facts_text}"},
        ],
        response_format=ResearchCoverageReport,
    )
    return checked_coverage(contract, response.choices[0].message.parsed, facts)


def answer_obligations(contract: QuestionAnswerContract) -> list[AnswerObligation]:
    primary = contract.primary_answer_obligation.model_copy(update={"is_primary": True, "is_required": True})
    return [primary, *[
        item.model_copy(update={"is_required": item.is_primary or item.is_required})
        for item in contract.required_supporting_obligations
    ]]


def checked_coverage(
    contract: QuestionAnswerContract, report: ResearchCoverageReport, facts: list[dict],
) -> ResearchCoverageReport:
    """Structured coverage and usable evidence IDs govern required support."""
    usable = {str(fact.get("id")) for fact in facts if fact_is_supported(fact)}
    by_id = {item.obligation_id: item for item in report.coverage}
    coverage = []
    missing = []
    for obligation in answer_obligations(contract):
        item = by_id.get(obligation.id) or ObligationCoverage(
            obligation_id=obligation.id, status="missing", reasoning="Coverage evaluation absent.",
        )
        ids = [identifier for identifier in item.supporting_fact_ids if identifier in usable]
        item = item.model_copy(update={"supporting_fact_ids": ids})
        if item.status == "satisfied" and not ids:
            item = item.model_copy(update={
                "status": "unsupported", "reasoning": "No supporting usable fact IDs.",
            })
        coverage.append(item)
        if obligation.is_required and item.status != "satisfied":
            missing.append(obligation.id)
    return report.model_copy(update={
        "coverage": coverage, "missing_obligations": missing, "is_sufficient": not missing,
    })
