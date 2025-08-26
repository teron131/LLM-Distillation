"""Context definitions for RAG CoT Generation: prompt templates and structured output models."""

from pydantic import BaseModel, Field

# Prompt templates from the paper
REASONING_PROMPT_TEMPLATE = """You are a reasoning question generator assistant. Your goal is to create a novel, and challenging reasoning question. You are provided the following seed questions:
Seed Question 1: {seed1}
Seed Question 2: {seed2}

Your task is to:
1. Write a brand-new, self-contained reasoning question that meets the following requirements:
   (a) The question draws inspiration from the seed question without copying it verbatim, remaining novel and of comparable difficulty.
   (b) The question's final answer should be a single, unambiguous scalar value (e.g., an integer, reduced fraction, exact radical), or another answer type that can be verified in one step (e.g., 'yes/no,' a choice from A to D).
2. Then reason step by step to solve the new question.

Please provide:
- question: The generated reasoning question
- reasoning_steps: Your step-by-step reasoning to solve the question
- final_answer: The final answer as a scalar value (just the answer, no additional formatting)"""

INSTRUCTION_PROMPT_TEMPLATE = """You are a prompt generator assistant. Your goal is to create diverse and creative synthetic prompts.
Please follow the steps below to create synthetic prompts.
Step 1: Carefully read #Prompt 1# and #Prompt 2#. Identify and list all the common elements between these two prompts. If no common elements are found, list the main elements from each prompt.
Step 2: Develop a comprehensive plan based on the #Common Elements List# or #Main Elements List# from Step 1. This plan will guide the generation of new synthetic prompts that are similar to the original prompts.
Step 3: Execute the plan step by step and provide one #Synthetic Prompt#.
Please reply strictly in the following format:
- Step 1 #Common Elements List# or #Main Elements List#:
- Step 2 #Plan#:
- Step 3 #Synthetic Prompt#:
#Prompt 1#:
{prompt1}
#Prompt 2#:
{prompt2}"""

# Quality evaluation prompt template
QUALITY_EVALUATION_PROMPT = """You are an expert evaluator tasked with scoring the quality of AI responses across multiple specific criteria. Please evaluate the following response and provide scores for each criterion.

Original Prompt:
{prompt}

Response to Evaluate:
{response}

Please score the response on a scale of 1-10 for each criterion:

1. **Accuracy** (1-10): How factually correct and accurate is the information? Are there any errors or misleading statements?

2. **Completeness** (1-10): How thorough and complete is the response? Does it adequately cover the topic or question asked?

3. **Relevance** (1-10): How relevant is the response to the original prompt? Does it stay on topic and address what was asked?

4. **Helpfulness** (1-10): How useful is this response to someone with this prompt? Does it address their needs effectively?

5. **Organization** (1-10): How well-structured and clearly organized is the content? Is it easy to follow and logically arranged?

6. **Grammar** (1-10): How good is the grammar, spelling, and overall writing quality? Are there typos or language errors?

Provide a score from 1-10 for each aspect where 1 is very poor and 10 is excellent."""


# Pydantic models for structured outputs


class ResponseScore(BaseModel):
    """Multi-aspect scoring model that evaluates separate criteria and returns a normalized final score."""

    accuracy: int = Field(description="Accuracy and correctness of information (1-10)", ge=1, le=10)
    completeness: int = Field(description="How complete and thorough the response is (1-10)", ge=1, le=10)
    relevance: int = Field(description="Relevance to the original prompt (1-10)", ge=1, le=10)
    helpfulness: int = Field(description="How helpful the response is to the user (1-10)", ge=1, le=10)
    organization: int = Field(description="Clarity and logical organization of content (1-10)", ge=1, le=10)
    grammar: int = Field(description="Grammar quality and absence of typos (1-10)", ge=1, le=10)


class ReasoningResponse(BaseModel):
    """Structured model for reasoning task output."""

    question: str = Field(description="The generated reasoning question")
    reasoning_steps: str = Field(description="Step-by-step reasoning to solve the question")
    final_answer: str = Field(description="The final answer as a scalar value (number, fraction, etc.)")


class AnswerOnlyResponse(BaseModel):
    """Structured model for answer-only responses during filtering."""

    final_answer: str = Field(description="The final answer as a scalar value (number, fraction, etc.)")
