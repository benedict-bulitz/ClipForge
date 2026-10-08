from __future__ import annotations

import logging
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, Field

from clipforge.config import Settings

logger = logging.getLogger("clipforge.contract")

class AnswerObligation(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    description: str = Field(description="What exactly must be answered or explained")
    is_primary: bool = Field(description="True if this is the core primary answer")
    is_required: bool = Field(description="True if the video must fail without this")
    fact_ids: list[str] = Field(default_factory=list, description="Fact IDs from research that support this")

class CausalChainStep(BaseModel):
    id: str
    description: str
    depth_level: int

class QuestionAnswerContract(BaseModel):
    question_type: Literal[
        "causal", "mechanistic", "historical_motive", "definition",
        "comparison", "quantity", "timeline", "consequence", "explanation", "mixed"
    ]
    core_question: str
    primary_answer_obligation: AnswerObligation
    required_supporting_obligations: list[AnswerObligation]
    optional_context: list[str] = Field(default_factory=list)
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
        "- For historical 'why' questions (e.g., 'Why was the Berlin Wall built?'): The immediate purpose/motive is MANDATORY. Broad political context alone is insufficient. Specify 'historical_motive' as the type.\n"
        "- For scientific 'why' questions (e.g., 'Why is Mars red?'): Focus on the direct cause/mechanism. Avoid circular answers (e.g., 'because of red dust' without explaining why the dust is red). Set 'minimum_answer_depth' to the level that genuinely resolves the curiosity.\n"
        "- Do not require maximum depth if a shorter explanation resolves the question.\n"
        "- Do not blindly treat every question as causal. Only require a causal chain if the question warrants it.\n"
    )
    response = client.beta.chat.completions.parse(
        model=settings.openai_director_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Question: {question}\nLanguage: {language}"},
        ],
        response_format=QuestionAnswerContract,
        temperature=0.0,
    )
    return response.choices[0].message.parsed

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
    
    facts_text = "\n".join([f"Fact ID {f.get('id')}: {f.get('claim')}" for f in facts])
    
    response = client.beta.chat.completions.parse(
        model=settings.openai_director_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Contract:\n{contract.model_dump_json(indent=2)}\n\nFacts:\n{facts_text}"},
        ],
        response_format=ResearchCoverageReport,
        temperature=0.0,
    )
    return response.choices[0].message.parsed
