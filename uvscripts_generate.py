import argparse
import json
import logging
import os
import random
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from dotenv import load_dotenv
from google import genai
from google.genai import types
from huggingface_hub import DatasetCard, login
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from sklearn.cluster import KMeans
from tqdm.auto import tqdm

from datasets import Dataset, load_dataset

# Enable HF Transfer for faster downloads
os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"

# Load environment variables
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Batch processing configuration
SAVE_BATCH_SIZE = 100  # Save every N generated samples
INFERENCE_BATCH_SIZE = 10  # Batch size for LLM inference
MAX_WORKERS = min(os.cpu_count(), INFERENCE_BATCH_SIZE)  # Number of concurrent workers
SAVE_DIR = Path("./generated_data")

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


# Pydantic model for response quality scoring
class ResponseScore(BaseModel):
    """Multi-aspect scoring model that evaluates separate criteria and returns a normalized final score."""

    accuracy: int = Field(description="Accuracy and correctness of information (1-10)", ge=1, le=10)
    completeness: int = Field(description="How complete and thorough the response is (1-10)", ge=1, le=10)
    relevance: int = Field(description="Relevance to the original prompt (1-10)", ge=1, le=10)
    helpfulness: int = Field(description="How helpful the response is to the user (1-10)", ge=1, le=10)
    organization: int = Field(description="Clarity and logical organization of content (1-10)", ge=1, le=10)
    grammar: int = Field(description="Grammar quality and absence of typos (1-10)", ge=1, le=10)


# Add new Pydantic models for structured reasoning output
class ReasoningResponse(BaseModel):
    """Structured model for reasoning task output."""

    question: str = Field(description="The generated reasoning question")
    reasoning_steps: str = Field(description="Step-by-step reasoning to solve the question")
    final_answer: str = Field(description="The final answer as a scalar value (number, fraction, etc.)")


class AnswerOnlyResponse(BaseModel):
    """Structured model for answer-only responses during filtering."""

    final_answer: str = Field(description="The final answer as a scalar value (number, fraction, etc.)")


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


def create_save_dir(output_dataset: str) -> Path:
    """Create directory for saving generated data."""
    save_path = SAVE_DIR / output_dataset.replace("/", "_")
    save_path.mkdir(parents=True, exist_ok=True)
    return save_path


def save_batch_jsonl(data: List[Dict], save_path: Path, stage: str, batch_num: int) -> None:
    """Save batch of data to JSONL file."""
    try:
        filename = f"{stage}_batch_{batch_num:04d}.jsonl"
        filepath = save_path / filename

        with open(filepath, "w", encoding="utf-8") as f:
            for item in data:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

        logger.info(f"Saved batch {batch_num} to {filename} ({len(data)} items)")

    except Exception as e:
        logger.warning(f"Failed to save batch: {e}")


def load_all_batches(save_path: Path, stage: str) -> List[Dict]:
    """Load all batches for a given stage."""
    all_data = []

    # Find all batch files for this stage
    batch_files = sorted(save_path.glob(f"{stage}_batch_*.jsonl"))

    for batch_file in batch_files:
        try:
            with open(batch_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        all_data.append(json.loads(line.strip()))
            logger.info(f"Loaded {batch_file.name}")
        except Exception as e:
            logger.warning(f"Failed to load {batch_file}: {e}")

    return all_data


def save_final_jsonl(data: List[Dict], save_path: Path, filename: str = "final_dataset.jsonl") -> Path:
    """Save final dataset to JSONL file."""
    try:
        filepath = save_path / filename

        with open(filepath, "w", encoding="utf-8") as f:
            for item in data:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

        logger.info(f"Final dataset saved: {filepath} ({len(data)} items)")
        return filepath

    except Exception as e:
        logger.error(f"Failed to save final dataset: {e}")
        raise


def check_openrouter_api_key() -> str:
    """Check if OpenRouter API key is available."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        logger.error("OPENROUTER_API_KEY environment variable is required.")
        logger.error("Please set your OpenRouter API key: export OPENROUTER_API_KEY=your_key")
        sys.exit(1)
    return api_key


def check_gemini_api_key() -> str:
    """Check if Gemini API key is available."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY environment variable is required for embeddings.")
        logger.error("Please set your Gemini API key: export GEMINI_API_KEY=your_key")
        sys.exit(1)
    return api_key


def get_llm(model: str, temperature: float = 0.7, max_tokens: int = 2048) -> ChatOpenAI:
    """Initialize OpenRouter LLM with specified parameters."""
    api_key = check_openrouter_api_key()

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        temperature=temperature,
        max_tokens=max_tokens,
    )


def get_gemini_client() -> genai.Client:
    """Initialize Gemini client for embeddings."""
    api_key = check_gemini_api_key()
    return genai.Client(api_key=api_key)


def parse_thinking_output(text: str) -> str:
    """Remove thinking tokens from model output."""
    # Remove <think>...</think> blocks
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text.strip()


def extract_reasoning_output(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract question and answer from reasoning task output.

    DEPRECATED: This function is deprecated for new reasoning tasks.
    Use structured output with ReasoningResponse model instead.
    Kept for backward compatibility with existing data.
    """
    text = parse_thinking_output(text)

    # Try multiple patterns to extract question and answer
    question = None
    answer = None

    # Pattern 1: Look for the structured format from the prompt
    question_patterns = [
        r"question:\s*(.+?)(?=reasoning_steps:|final_answer:|\n\n|\Z)",
        r"\*\*question\*\*:\s*(.+?)(?=\*\*reasoning_steps\*\*:|\*\*final_answer\*\*:|\n\n|\Z)",
        r"- question:\s*(.+?)(?=- reasoning_steps:|- final_answer:|\n\n|\Z)",
        r"\[New Question Begin\](.*?)\[New Question End\]",
        r"(?:Generated|New)\s+(?:Question|Problem):\s*(.+?)(?=\n\n|Answer:|Solution:|\Z)",
    ]

    answer_patterns = [
        r"final_answer:\s*(.+?)(?=\n|\Z)",
        r"\*\*final_answer\*\*:\s*(.+?)(?=\n|\Z)",
        r"- final_answer:\s*(.+?)(?=\n|\Z)",
        r"\[Final Answer to New Question Begin\]\\?boxed\{(.*?)\}\[Final Answer to New Question End\]",
        r"\[Final Answer to New Question Begin\](.*?)\[Final Answer to New Question End\]",
        r"(?:Final\s+)?(?:Answer|Solution):\s*(.+?)(?=\n|\Z)",
        r"\\boxed\{([^}]+)\}",
        r"\$([^$]+)\$(?:\s*$)",
    ]

    # Try to find question
    for pattern in question_patterns:
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            question = match.group(1).strip()
            break

    # Try to find answer
    for pattern in answer_patterns:
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            answer = match.group(1).strip()
            break

    # If we still don't have both, try a more aggressive approach
    if not question or not answer:
        # Split text into lines and look for structured content
        lines = [line.strip() for line in text.split("\n") if line.strip()]

        for i, line in enumerate(lines):
            # Look for question indicators
            if not question and any(indicator in line.lower() for indicator in ["question:", "problem:", "generated question"]):
                # Take the rest of the line or next few lines as question
                question_text = line.split(":", 1)[-1].strip()
                if not question_text and i + 1 < len(lines):
                    question_text = lines[i + 1]
                if question_text:
                    question = question_text

            # Look for answer indicators
            if not answer and any(indicator in line.lower() for indicator in ["answer:", "final answer:", "solution:"]):
                # Take the rest of the line as answer
                answer_text = line.split(":", 1)[-1].strip()
                if answer_text:
                    answer = answer_text

    return question, answer


def extract_instruction_output(text: str) -> Optional[str]:
    """Extract synthetic prompt from instruction task output."""
    text = parse_thinking_output(text)

    # Look for the synthetic prompt after "Step 3 #Synthetic Prompt#:"
    match = re.search(r"Step 3 #Synthetic Prompt#:\s*(.+)", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def categorize_prompts(prompts: List[str], num_categories: int = 8) -> Dict[int, List[int]]:
    """Categorize prompts using Gemini embeddings and clustering for instruction tasks."""
    logger.info(f"Categorizing {len(prompts)} prompts into {num_categories} categories using Gemini embeddings...")

    # Initialize Gemini client
    gemini_client = get_gemini_client()

    # Get embeddings using Gemini API
    logger.info("Generating embeddings with Gemini...")
    try:
        result = gemini_client.models.embed_content(model="gemini-embedding-001", contents=prompts, config=types.EmbedContentConfig(task_type="CLUSTERING", output_dimensionality=768))

        # Extract embeddings and normalize them
        embeddings = []
        for embedding_obj in result.embeddings:
            embedding_values = np.array(embedding_obj.values)
            # Normalize for consistent similarity calculations
            normalized_embedding = embedding_values / np.linalg.norm(embedding_values)
            embeddings.append(normalized_embedding)

    except Exception as e:
        logger.error(f"Failed to generate embeddings with Gemini: {e}")
        logger.error("Please check your GEMINI_API_KEY and try again")
        sys.exit(1)

    # Cluster embeddings
    logger.info("Clustering embeddings...")
    embeddings_matrix = np.array(embeddings)
    kmeans = KMeans(n_clusters=num_categories, random_state=42)
    labels = kmeans.fit_predict(embeddings_matrix)

    # Group by category
    categories = {}
    for idx, label in enumerate(labels):
        if label not in categories:
            categories[label] = []
        categories[label].append(idx)

    logger.info(f"Created {len(categories)} categories with sizes: {[len(cat) for cat in categories.values()]}")
    return categories


def process_generation_batch(prompts_batch: List[str], llm: ChatOpenAI, task_type: str, seed_data: List[Dict]) -> List[Dict]:
    """Process a batch of generation prompts using individual inference to avoid JSON concatenation issues."""
    results = []

    try:
        # For reasoning tasks, use structured output with individual requests
        if task_type == "reasoning":
            # Try structured output first
            try:
                structured_llm = llm.with_structured_output(ReasoningResponse, method="function_calling")
                use_structured = True
            except Exception as e:
                logger.warning(f"Structured output not supported, falling back to text parsing: {e}")
                use_structured = False

            for i, prompt in enumerate(prompts_batch):
                try:
                    if use_structured:
                        # Try structured output
                        response = structured_llm.invoke([HumanMessage(content=prompt)])

                        if response and hasattr(response, "question") and hasattr(response, "final_answer"):
                            # Validate that the response has meaningful content
                            if response.question.strip() and response.final_answer.strip():
                                results.append(
                                    {
                                        "question": response.question,
                                        "answer": response.final_answer,  # Store only the final answer for consistency
                                        "reasoning_steps": response.reasoning_steps,  # Store reasoning separately
                                        "metadata": {"prompt_index": i},
                                    }
                                )
                            else:
                                logger.warning(f"Skipping response with empty question or answer")
                            # Fall back to text parsing for this response
                            use_structured = False

                    if not use_structured:
                        # Fall back to text-based parsing
                        response = llm.invoke([HumanMessage(content=prompt)])
                        output_text = response.content
                        question, answer = extract_reasoning_output(output_text)

                        if question and answer:
                            results.append(
                                {
                                    "question": question,
                                    "answer": answer,
                                    "reasoning_steps": output_text,  # Store full output as reasoning
                                    "metadata": {"prompt_index": i},
                                }
                            )
                        else:
                            logger.warning(f"Failed to extract question/answer from text output")

                except Exception as e:
                    logger.warning(f"Individual generation failed for prompt {i}: {e}")
                    continue
        else:
            # For instruction tasks, use original text-based approach with batch processing
            # Convert prompts to HumanMessage format for batch processing
            messages_batch = [[HumanMessage(content=prompt)] for prompt in prompts_batch]

            # Use batch inference
            responses = llm.batch(messages_batch)

            for i, response in enumerate(responses):
                output_text = response.content
                synthetic_prompt = extract_instruction_output(output_text)
                if synthetic_prompt:
                    results.append(
                        {
                            "prompt": synthetic_prompt,
                            "metadata": {"prompt_index": i},
                        }
                    )

        return results

    except Exception as e:
        logger.warning(f"Batch generation failed: {e}")
        return []


def generate_synthetic_data(
    llm: ChatOpenAI,
    seed_data: List[Dict],
    task_type: str,
    num_samples: int,
    categories: Optional[Dict[int, List[int]]] = None,
    save_path: Optional[Path] = None,
    max_workers: int = MAX_WORKERS,
) -> List[Dict]:
    """Generate synthetic data using CoT-Self-Instruct with concurrent batch processing."""
    logger.info(f"Generating {num_samples} samples with {max_workers} workers and batch size {INFERENCE_BATCH_SIZE}")

    # Prepare all prompts first
    all_prompts = []
    all_seed_pairs = []

    for _ in range(num_samples):
        # Sample seed data
        if task_type == "reasoning":
            seeds = random.sample(seed_data, min(2, len(seed_data)))
            prompt = REASONING_PROMPT_TEMPLATE.format(seed1=seeds[0].get("question", seeds[0].get("prompt", "")), seed2=seeds[1].get("question", seeds[1].get("prompt", "")) if len(seeds) > 1 else seeds[0].get("question", seeds[0].get("prompt", "")))
        else:
            # Category-aware sampling for instruction tasks
            if categories:
                category = random.choice(list(categories.keys()))
                category_indices = categories[category]
                indices = random.sample(category_indices, min(2, len(category_indices)))
                seeds = [seed_data[i] for i in indices]
            else:
                seeds = random.sample(seed_data, min(2, len(seed_data)))

            prompt = INSTRUCTION_PROMPT_TEMPLATE.format(prompt1=seeds[0].get("prompt", seeds[0].get("question", "")), prompt2=seeds[1].get("prompt", seeds[1].get("question", "")) if len(seeds) > 1 else seeds[0].get("prompt", seeds[0].get("question", "")))

        all_prompts.append(prompt)
        all_seed_pairs.append([seed_data.index(s) for s in seeds])

    # Process in batches with concurrent execution
    synthetic_data = []
    batch_num = 0

    # Split prompts into batches for inference
    prompt_batches = [all_prompts[i : i + INFERENCE_BATCH_SIZE] for i in range(0, len(all_prompts), INFERENCE_BATCH_SIZE)]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit batches for processing
        future_to_batch = {executor.submit(process_generation_batch, batch, llm, task_type, seed_data): (i, batch) for i, batch in enumerate(prompt_batches)}

        # Process results with progress bar
        with tqdm(total=len(prompt_batches), desc="Processing generation batches") as pbar:
            for future in as_completed(future_to_batch):
                batch_idx, batch = future_to_batch[future]
                try:
                    batch_results = future.result()

                    # Add seed indices to results metadata
                    for result in batch_results:
                        prompt_idx = batch_idx * INFERENCE_BATCH_SIZE + result["metadata"]["prompt_index"]
                        result["metadata"]["seed_indices"] = all_seed_pairs[prompt_idx]
                        del result["metadata"]["prompt_index"]  # Remove temporary index
                        synthetic_data.append(result)

                    # Save batch if we have enough data
                    if save_path and len(synthetic_data) >= (batch_num + 1) * SAVE_BATCH_SIZE:
                        batch_start = batch_num * SAVE_BATCH_SIZE
                        batch_end = (batch_num + 1) * SAVE_BATCH_SIZE
                        save_batch_jsonl(synthetic_data[batch_start:batch_end], save_path, "generation", batch_num)
                        batch_num += 1

                except Exception as e:
                    logger.warning(f"Batch processing failed: {e}")

                pbar.update(1)

    # Save final batch if there are remaining items
    if save_path and len(synthetic_data) % SAVE_BATCH_SIZE != 0:
        batch_start = (len(synthetic_data) // SAVE_BATCH_SIZE) * SAVE_BATCH_SIZE
        batch_data = synthetic_data[batch_start:]
        save_batch_jsonl(batch_data, save_path, "generation", batch_num)

    logger.info(f"Generated {len(synthetic_data)} synthetic examples")
    return synthetic_data


def process_consistency_item(args: Tuple[Dict, ChatOpenAI, int, float]) -> Optional[Dict]:
    """Process a single item for answer consistency filtering."""
    item, llm, k_responses, threshold = args

    question = item["question"]
    original_answer = item["answer"]

    # Debug logging
    logger.debug(f"Processing consistency for question: {question[:100]}...")
    logger.debug(f"Original answer: {original_answer}")

    # Create structured LLM for consistent answer extraction
    structured_llm = llm.with_structured_output(AnswerOnlyResponse, method="function_calling")

    # Create a smart prompt that handles format flexibility
    answer_prompt = f"""Solve this step by step and provide only the final answer. 

Important: Your answer should match the expected format. If the expected answer is a percentage, provide a percentage. If it's a dollar amount, provide a dollar amount. If it's just a number, provide just the number.

Question: {question}

Provide only the final answer in the most appropriate format."""

    # Generate K responses using individual requests
    answers = []
    for _ in range(k_responses):
        try:
            response = structured_llm.invoke([HumanMessage(content=answer_prompt)])
            if hasattr(response, "final_answer") and response.final_answer:
                answers.append(response.final_answer.strip())
        except Exception as e:
            logger.debug(f"Individual answer generation failed: {e}")
            continue

    logger.debug(f"Generated {len(answers)} answers: {answers}")

    if not answers:
        logger.debug("No valid answers generated")
        return None

    # Use a smart LLM to judge answer equivalence instead of manual normalization
    equivalence_prompt = f"""Compare these answers to determine if they are equivalent, considering different valid formats (e.g., "15%" vs "15 percent" vs "0.15", or "$1.2M" vs "$1,200,000").

Original answer: {original_answer}
Generated answers: {answers}

For each generated answer, determine if it's equivalent to the original answer. Consider:
- Percentage formats (15%, 15 percent, 0.15 if representing 15%)
- Currency formats ($1M, $1,000,000, 1 million dollars)
- Number formats (fifteen, 15, 15.0)
- Mathematical equivalence

Count how many of the generated answers are equivalent to the original answer.
Return only the count as a number."""

    try:
        equivalence_response = llm.invoke([HumanMessage(content=equivalence_prompt)])
        equivalent_count_text = equivalence_response.content.strip()

        # Extract number from response
        import re

        count_match = re.search(r"(\d+)", equivalent_count_text)
        if count_match:
            equivalent_count = int(count_match.group(1))
        else:
            logger.debug(f"Could not parse equivalence count from: {equivalent_count_text}")
            return None

        consistency_ratio = equivalent_count / len(answers)

        logger.debug(f"Equivalent answers: {equivalent_count}/{len(answers)} = {consistency_ratio:.2f}")

        # Check if consistency meets threshold
        if consistency_ratio >= threshold:
            logger.debug("Item passed consistency filter")
            # Add consistency metadata
            if "metadata" not in item:
                item["metadata"] = {}
            item["metadata"]["consistency_score"] = consistency_ratio
            item["metadata"]["consistency_responses"] = len(answers)
            item["metadata"]["equivalent_count"] = equivalent_count
            item["metadata"]["all_answers"] = answers[:5]  # Store first 5 answers for debugging
            item["metadata"]["original_answer"] = original_answer
            return item
        else:
            logger.debug(f"Item failed consistency filter: ratio={consistency_ratio:.2f} >= {threshold}")

    except Exception as e:
        logger.warning(f"Answer equivalence evaluation failed: {e}")
        return None

    return None


def answer_consistency_filter(
    llm: ChatOpenAI,
    synthetic_data: List[Dict],
    k_responses: int = 16,
    threshold: float = 0.5,
    save_path: Optional[Path] = None,
    max_workers: int = MAX_WORKERS,
    save_batch_size: int = 100,
) -> List[Dict]:
    """Filter reasoning tasks using Answer-Consistency with concurrent processing."""
    logger.info(f"Applying Answer-Consistency filter with K={k_responses} using {max_workers} workers")
    logger.info(f"Input data: {len(synthetic_data)} examples")

    # Debug: Log first example to verify data flow
    if synthetic_data:
        first_example = synthetic_data[0]
        logger.info(f"First example question: {first_example.get('question', 'NO QUESTION')[:100]}...")
        logger.info(f"First example answer: {first_example.get('answer', 'NO ANSWER')}")

    # Prepare arguments for concurrent processing
    args_list = [(item, llm, k_responses, threshold) for item in synthetic_data]

    filtered_data = []
    batch_num = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Process items concurrently with progress bar
        futures = [executor.submit(process_consistency_item, args) for args in args_list]

        for future in tqdm(as_completed(futures), total=len(futures), desc="Answer-Consistency filtering"):
            try:
                result = future.result()
                if result is not None:
                    filtered_data.append(result)

                    # Save batch when we reach save_batch_size
                    if save_path and len(filtered_data) % save_batch_size == 0:
                        batch_start = len(filtered_data) - save_batch_size
                        batch_data = filtered_data[batch_start:]
                        save_batch_jsonl(batch_data, save_path, "answer_consistency", batch_num)
                        batch_num += 1

            except Exception as e:
                logger.warning(f"Consistency filtering failed: {e}")

    # Save final batch if there are remaining items
    if save_path and len(filtered_data) % save_batch_size != 0:
        batch_start = (len(filtered_data) // save_batch_size) * save_batch_size
        batch_data = filtered_data[batch_start:]
        save_batch_jsonl(batch_data, save_path, "answer_consistency", batch_num)

    logger.info(f"Answer-Consistency: kept {len(filtered_data)}/{len(synthetic_data)} examples")
    return filtered_data


def process_rip_item(args: Tuple[Dict, ChatOpenAI, ChatOpenAI, int, float]) -> Optional[Dict]:
    """Process a single item for RIP filtering."""
    item, llm, evaluator_llm, k_responses, threshold = args

    prompt = item.get("prompt", item.get("question", ""))

    # Generate K responses using batch inference
    prompts = [prompt] * k_responses
    messages_batch = [[HumanMessage(content=p)] for p in prompts]

    try:
        responses = llm.batch(messages_batch)

        # Evaluate response quality for each response
        quality_scores = []
        for response in responses:
            response_text = response.content
            quality_score = evaluate_response_quality(evaluator_llm, prompt, response_text)
            if quality_score is not None:
                quality_scores.append(quality_score)

        # Calculate minimum quality score as the quality indicator
        if quality_scores:
            min_score = min(quality_scores)
            avg_score = sum(quality_scores) / len(quality_scores)
            max_score = max(quality_scores)
            std_score = np.std(quality_scores) if len(quality_scores) > 1 else 0.0

            # Use minimum score to ensure all responses meet quality threshold
            if min_score >= threshold:
                # Add RIP metadata
                if "metadata" not in item:
                    item["metadata"] = {}
                item["metadata"]["rip_min_score"] = min_score
                item["metadata"]["rip_avg_score"] = avg_score
                item["metadata"]["rip_max_score"] = max_score
                item["metadata"]["rip_std_score"] = std_score
                item["metadata"]["rip_scores_count"] = len(quality_scores)
                item["metadata"]["rip_threshold"] = threshold

                # Add detailed quality metrics
                action_item = item  # Make a copy

                # Calculate average scores for each aspect
                avg_accuracy = sum(s["accuracy"] for s in quality_scores) / len(quality_scores)
                avg_completeness = sum(s["completeness"] for s in quality_scores) / len(quality_scores)
                avg_relevance = sum(s["relevance"] for s in quality_scores) / len(quality_scores)
                avg_helpfulness = sum(s["helpfulness"] for s in quality_scores) / len(quality_scores)
                avg_organization = sum(s["organization"] for s in quality_scores) / len(quality_scores)
                avg_grammar = sum(s["grammar"] for s in quality_scores) / len(quality_scores)

                action_item["metadata"]["rip_avg_accuracy"] = avg_accuracy
                action_item["metadata"]["rip_avg_completeness"] = avg_completeness
                action_item["metadata"]["rip_avg_relevance"] = avg_relevance
                action_item["metadata"]["rip_avg_helpfulness"] = avg_helpfulness
                action_item["metadata"]["rip_avg_organization"] = avg_organization
                action_item["metadata"]["rip_avg_grammar"] = avg_grammar

                # Add individual score ranges for detailed analysis
                action_item["metadata"]["rip_accuracy_range"] = [min(s["accuracy"] for s in quality_scores), max(s["accuracy"] for s in quality_scores)]
                action_item["metadata"]["rip_completeness_range"] = [min(s["completeness"] for s in quality_scores), max(s["completeness"] for s in quality_scores)]
                action_item["metadata"]["rip_relevance_range"] = [min(s["relevance"] for s in quality_scores), max(s["relevance"] for s in quality_scores)]
                action_item["metadata"]["rip_helpfulness_range"] = [min(s["helpfulness"] for s in quality_scores), max(s["helpfulness"] for s in quality_scores)]
                action_item["metadata"]["rip_organization_range"] = [min(s["organization"] for s in quality_scores), max(s["organization"] for s in quality_scores)]
                action_item["metadata"]["rip_grammar_range"] = [min(s["grammar"] for s in quality_scores), max(s["grammar"] for s in quality_scores)]

                return action_item

    except Exception as e:
        logger.warning(f"RIP filtering failed: {e}")

    return None


def rip_filter(
    llm: ChatOpenAI,
    synthetic_data: List[Dict],
    reward_model_id: str,
    k_responses: int = 32,
    threshold: float = 6.0,
    save_path: Optional[Path] = None,
    max_workers: int = MAX_WORKERS,
    save_batch_size: int = 100,
) -> List[Dict]:
    """Filter using Rejecting Instruction Preferences (RIP) with concurrent multi-aspect quality evaluation."""
    logger.info(f"Applying RIP filter with K={k_responses} using {max_workers} workers")
    logger.info(f"Quality threshold: {threshold}/10.0 (responses below this normalized score will be rejected)")

    # Create evaluator LLM for quality scoring
    evaluator_llm = get_llm("gpt-4o-mini", temperature=0.1, max_tokens=512)

    # Prepare arguments for concurrent processing
    args_list = [(item, llm, evaluator_llm, k_responses, threshold) for item in synthetic_data]

    filtered_data = []
    batch_num = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Process items concurrently with progress bar
        futures = [executor.submit(process_rip_item, args) for args in args_list]

        for future in tqdm(as_completed(futures), total=len(futures), desc="RIP filtering with multi-aspect evaluation"):
            try:
                result = future.result()
                if result is not None:
                    filtered_data.append(result)

                    # Save batch when we reach save_batch_size
                    if save_path and len(filtered_data) % save_batch_size == 0:
                        batch_start = len(filtered_data) - save_batch_size
                        batch_data = filtered_data[batch_start:]
                        save_batch_jsonl(batch_data, save_path, "rip", batch_num)
                        batch_num += 1

            except Exception as e:
                logger.warning(f"RIP filtering failed: {e}")

    # Save final batch if there are remaining items
    if save_path and len(filtered_data) % save_batch_size != 0:
        batch_start = (len(filtered_data) // save_batch_size) * save_batch_size
        batch_data = filtered_data[batch_start:]
        save_batch_jsonl(batch_data, save_path, "rip", batch_num)

    logger.info(f"RIP filter: kept {len(filtered_data)}/{len(synthetic_data)} examples")
    return filtered_data


def evaluate_response_quality(
    evaluator_llm: ChatOpenAI,
    prompt: str,
    response: str,
) -> Optional[Dict]:
    """Evaluate response quality using structured output, returning detailed scores."""
    try:
        # Create structured LLM for quality evaluation
        structured_llm = evaluator_llm.with_structured_output(ResponseScore, method="function_calling")

        evaluation_prompt = QUALITY_EVALUATION_PROMPT.format(prompt=prompt, response=response)

        result = structured_llm.invoke([HumanMessage(content=evaluation_prompt)])

        # Calculate final normalized score
        # Sum all aspect scores and normalize to 0-10 scale
        total_score = result.accuracy + result.helpfulness + result.organization + result.grammar + result.completeness + result.relevance

        # Normalize: 6 aspects × 10 max score = 60 max total
        # Normalize to 0-10 scale: (total_score / 60) * 10
        final_score = (total_score / 60.0) * 10.0

        return {"final_score": final_score, "accuracy": result.accuracy, "completeness": result.completeness, "relevance": result.relevance, "helpfulness": result.helpfulness, "organization": result.organization, "grammar": result.grammar, "total_raw_score": total_score}

    except Exception as e:
        logger.warning(f"Quality evaluation failed: {e}")
        return None


def create_dataset_card(
    task_type: str,
    source_dataset: str,
    generation_model: str,
    filter_method: str,
    num_generated: int,
    num_filtered: int,
    generation_time: str,
    additional_info: Dict = None,
) -> str:
    """Create a comprehensive dataset card."""
    filter_info = ""
    if filter_method == "answer-consistency":
        filter_info = """
### Answer-Consistency Filtering
This dataset was filtered using Answer-Consistency:
- Generated K responses for each synthetic question
- Kept only examples where majority answer matched the generated answer
- Ensures high-quality, correctly solved problems"""
    elif filter_method == "rip":
        filter_info = """
### RIP (Rejecting Instruction Preferences) Filtering
This dataset was filtered using RIP:
- Generated K responses for each synthetic prompt
- Scored responses using multi-aspect quality evaluation
- Kept only prompts with high minimum scores"""

    # Calculate acceptance rate safely
    acceptance_rate = (num_filtered / num_generated * 100) if num_generated > 0 else 0.0

    return f"""---
tags:
- synthetic-data
- cot-self-instruct
- {task_type}
- uv-script
- openrouter-api
- gemini-embeddings
- concurrent-processing
---
# CoT-Self-Instruct Synthetic Data
This dataset contains synthetic {task_type} data generated using the Chain-of-Thought Self-Instruct methodology via OpenRouter API with Gemini embeddings for clustering and concurrent processing.
## Generation Details
- **Source Dataset**: [{source_dataset}](https://huggingface.co/datasets/{source_dataset})
- **Generation Model**: {generation_model} (via OpenRouter API)
- **Embedding Model**: gemini-embedding-001 (for prompt clustering)
- **Task Type**: {task_type}
- **Filter Method**: {filter_method}
- **Generated Examples**: {num_generated:,}
- **After Filtering**: {num_filtered:,} ({acceptance_rate:.1f}% acceptance rate)
- **Generation Date**: {generation_time}
{filter_info}
## Methodology
Generated using CoT-Self-Instruct with concurrent processing and JSONL batch saving, which:
1. Uses Gemini embeddings to cluster seed prompts for better sampling diversity
2. Uses Chain-of-Thought reasoning to analyze seed examples
3. Generates new synthetic examples with concurrent batch inference
4. Applies quality filtering with parallel processing
5. Saves data locally in JSONL format during processing
6. Uses ThreadPoolExecutor for concurrent processing and tqdm for progress tracking
Based on the paper: "CoT-Self-Instruct: Building high-quality synthetic prompts for reasoning and non-reasoning tasks" (2025)
## Generation Script
Generated using the CoT-Self-Instruct script from [uv-scripts/synthetic-data](https://huggingface.co/datasets/uv-scripts/synthetic-data).
To reproduce:
```bash
export OPENROUTER_API_KEY=your_openrouter_key
export GEMINI_API_KEY=your_gemini_key
uv run https://huggingface.co/datasets/uv-scripts/synthetic-data/raw/main/cot-self-instruct.py \\
    --seed-dataset {source_dataset} \\
    --output-dataset <your-dataset> \\
    --task-type {task_type} \\
    --generation-model {generation_model} \\
    --filter-method {filter_method}
```
"""


def main():
    parser = argparse.ArgumentParser(
        description="Generate synthetic data using CoT-Self-Instruct via OpenRouter API with concurrent processing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Dataset arguments
    parser.add_argument(
        "--seed-dataset",
        type=str,
        required=True,
        help="HuggingFace dataset ID containing seed examples",
    )
    parser.add_argument(
        "--output-dataset",
        type=str,
        required=True,
        help="HuggingFace dataset ID for output",
    )

    # Task configuration
    parser.add_argument(
        "--task-type",
        type=str,
        choices=["reasoning", "instruction", "auto"],
        default="auto",
        help="Type of task (reasoning generates Q&A, instruction generates prompts)",
    )
    parser.add_argument(
        "--task-column",
        type=str,
        default=None,
        help="Column name containing tasks (auto-detected if not specified)",
    )

    # Model configuration
    parser.add_argument(
        "--generation-model",
        type=str,
        default="google/gemini-2.5-flash-lite",
        help="Model for synthetic data generation (via OpenRouter)",
    )
    parser.add_argument(
        "--filter-model",
        type=str,
        default="google/gemini-2.5-flash-lite",
        help="Model for filtering (defaults to generation model)",
    )
    parser.add_argument(
        "--reward-model",
        type=str,
        default="google/gemini-2.5-flash-lite",
        help="Reward model for RIP filtering",
    )

    # Generation parameters
    parser.add_argument(
        "--num-samples",
        type=int,
        default=5000,
        help="Number of synthetic examples to generate",
    )
    parser.add_argument(
        "--generation-temperature",
        type=float,
        default=0.7,
        help="Temperature for generation",
    )
    parser.add_argument(
        "--generation-max-tokens",
        type=int,
        default=2048,
        help="Max tokens for generation",
    )

    # Filtering parameters
    parser.add_argument(
        "--filter-method",
        type=str,
        choices=["answer-consistency", "rip", "both", "none"],
        default="both",
        help="Quality filtering method",
    )
    parser.add_argument(
        "--k-responses",
        type=int,
        default=16,
        help="Number of responses for filtering",
    )
    parser.add_argument(
        "--quality-threshold",
        type=float,
        default=0.5,
        help="Minimum quality threshold for filtering",
    )
    parser.add_argument(
        "--filter-temperature",
        type=float,
        default=0.6,
        help="Temperature for filtering",
    )
    parser.add_argument(
        "--filter-max-tokens",
        type=int,
        default=1024,
        help="Max tokens for filtering",
    )

    # Concurrent processing parameters
    parser.add_argument(
        "--save-batch-size",
        type=int,
        default=100,
        help="Batch size for saving JSONL files",
    )
    parser.add_argument(
        "--inference-batch-size",
        type=int,
        default=32,
        help="Batch size for LLM inference",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=os.cpu_count(),
        help="Maximum number of concurrent workers",
    )
    parser.add_argument(
        "--local-save-only",
        action="store_true",
        help="Only save locally, do not upload to HuggingFace Hub",
    )

    # Other arguments
    parser.add_argument(
        "--hf-token",
        type=str,
        default=None,
        help="HuggingFace API token",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed",
    )

    args = parser.parse_args()

    # Use local variables instead of modifying globals
    save_batch_size = args.save_batch_size
    inference_batch_size = args.inference_batch_size
    max_workers = args.max_workers

    logger.info(f"Using {max_workers} workers, inference batch size {inference_batch_size}, save batch size {save_batch_size}")

    # Set random seeds
    random.seed(args.seed)
    np.random.seed(args.seed)

    # Check API keys
    check_openrouter_api_key()
    # Only check Gemini API key if we might need embeddings (instruction tasks with clustering)
    if args.task_type in ["instruction", "auto"]:
        check_gemini_api_key()

    # Create save directory
    save_path = create_save_dir(args.output_dataset)
    logger.info(f"Saving batches to: {save_path}")

    # Authentication for HuggingFace (only if not local-only)
    if not args.local_save_only:
        hf_token = args.hf_token or os.environ.get("HF_TOKEN")
        if hf_token:
            login(token=hf_token)
        else:
            logger.warning("No HF_TOKEN provided - will only save locally")
            args.local_save_only = True

    # Load seed dataset
    logger.info(f"Loading seed dataset: {args.seed_dataset}")
    seed_dataset = load_dataset(args.seed_dataset, split="train")

    # Auto-detect task type and column if needed
    if args.task_type == "auto":
        columns = seed_dataset.column_names
        if "question" in columns and "answer" in columns:
            args.task_type = "reasoning"
            logger.info("Auto-detected task type: reasoning")
        else:
            args.task_type = "instruction"
            logger.info("Auto-detected task type: instruction")

    if not args.task_column:
        if args.task_type == "reasoning":
            args.task_column = "question"
        else:
            # Try to find prompt column
            for col in ["prompt", "instruction", "text", "input"]:
                if col in seed_dataset.column_names:
                    args.task_column = col
                    break

    logger.info(f"Using task column: {args.task_column}")

    # Convert to list of dicts
    seed_data = seed_dataset.to_list()

    # Categorize prompts for instruction tasks using Gemini embeddings
    categories = None
    if args.task_type == "instruction" and len(seed_data) > 100:
        prompts = [item.get(args.task_column, "") for item in seed_data]
        categories = categorize_prompts(prompts)

    # Initialize generation model
    logger.info(f"Initializing generation model: {args.generation_model}")
    generation_temperature = args.generation_temperature
    if args.task_type == "instruction":
        generation_temperature = 0.8  # Higher temperature for instruction tasks

    generation_llm = get_llm(args.generation_model, temperature=generation_temperature, max_tokens=args.generation_max_tokens)

    # Generate synthetic data with concurrent batch processing
    start_time = datetime.now()
    synthetic_data = generate_synthetic_data(
        generation_llm,
        seed_data,
        args.task_type,
        args.num_samples,
        categories,
        save_path,
        max_workers,
    )

    # Apply filtering with concurrent processing
    filter_model = args.filter_model or args.generation_model
    logger.info(f"Using filter model: {filter_model}")

    filter_llm = get_llm(filter_model, temperature=args.filter_temperature, max_tokens=args.filter_max_tokens)

    filtered_data = synthetic_data
    if args.filter_method != "none":
        if args.filter_method == "answer-consistency" and args.task_type == "reasoning":
            filtered_data = answer_consistency_filter(
                filter_llm,
                synthetic_data,
                args.k_responses,
                args.quality_threshold,
                save_path,
                max_workers,
                save_batch_size,
            )
        elif args.filter_method == "rip":
            # For RIP, update threshold for normalized scoring (default 6.0/10)
            rip_threshold = args.quality_threshold if args.quality_threshold > 1.0 else 6.0
            filtered_data = rip_filter(
                filter_llm,
                synthetic_data,
                args.reward_model,
                args.k_responses,
                rip_threshold,
                save_path,
                max_workers,
                save_batch_size,
            )
        elif args.filter_method == "both":
            if args.task_type == "reasoning":
                filtered_data = answer_consistency_filter(
                    filter_llm,
                    synthetic_data,
                    args.k_responses,
                    args.quality_threshold,
                    save_path,
                    max_workers,
                    save_batch_size,
                )
            rip_threshold = args.quality_threshold if args.quality_threshold > 1.0 else 6.0
            filtered_data = rip_filter(
                filter_llm,
                filtered_data,
                args.reward_model,
                args.k_responses,
                rip_threshold,
                save_path,
                max_workers,
                save_batch_size,
            )

    # Save final dataset locally first
    logger.info(f"Creating final dataset with {len(filtered_data)} examples")
    local_file_path = save_final_jsonl(filtered_data, save_path)

    # Create HuggingFace dataset from local file
    dataset = Dataset.from_list(filtered_data)

    # Create dataset card
    generation_time = start_time.strftime("%Y-%m-%d %H:%M:%S UTC")
    dataset_card = create_dataset_card(
        args.task_type,
        args.seed_dataset,
        args.generation_model,
        args.filter_method,
        len(synthetic_data),
        len(filtered_data),
        generation_time,
    )

    if not args.local_save_only:
        try:
            # Push to hub
            logger.info(f"Pushing dataset to: {args.output_dataset}")
            dataset.push_to_hub(args.output_dataset)

            # Push card separately
            card = DatasetCard(dataset_card)
            card.push_to_hub(args.output_dataset)

            logger.info("Done! Dataset available at: https://huggingface.co/datasets/" + args.output_dataset)
        except Exception as e:
            logger.error(f"Failed to upload to HuggingFace Hub: {e}")
            logger.info(f"Dataset saved locally at: {local_file_path}")
    else:
        logger.info(f"Dataset saved locally only at: {local_file_path}")

    # Print example usage command
    if len(sys.argv) > 1:
        print("\nTo run with concurrent processing and batch inference:")
        print(
            f"""export OPENROUTER_API_KEY=your_openrouter_key
export GEMINI_API_KEY=your_gemini_key
uv run cot-self-instruct.py \\
    --seed-dataset {args.seed_dataset} \\
    --output-dataset {args.output_dataset} \\
    --task-type {args.task_type} \\
    --generation-model {args.generation_model} \\
    --filter-method {args.filter_method} \\
    --num-samples {args.num_samples} \\
    --save-batch-size {save_batch_size} \\
    --inference-batch-size {inference_batch_size} \\
    --max-workers {max_workers}"""
        )


if __name__ == "__main__":
    main()
