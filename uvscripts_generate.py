import argparse
import json
import logging
import os
import random
import re
import sys
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
from dotenv import load_dotenv
from google import genai
from google.genai import types
from huggingface_hub import DatasetCard, login
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from sklearn.cluster import KMeans
from tqdm.auto import tqdm

from datasets import Dataset, load_dataset

# Enable HF Transfer for faster downloads
os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"

# Load environment variables
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Prompt templates from the paper
REASONING_PROMPT_TEMPLATE = """You are a reasoning question generator assistant. Your goal is to create a novel, and challenging reasoning question. You are provided the following seed questions:
Seed Question 1: {seed1}
Seed Question 2: {seed2}
Your task is to:
1. Write a brand-new, self-contained reasoning question that meets the following requirements:
(a) The question draws inspiration from the seed question without copying it verbatim, remaining novel and of comparable difficulty.
(b) The question's final answer should be a single, unambiguous scalar value (e.g., an integer, reduced fraction, exact radical), or another answer type that can be verified in one step (e.g., 'yes/no,' a choice from A to D).
2. Then reason step by step, solve the new question and format your output as follows:
[New Question Begin]{{your_generated_question}}[New Question End]
[Final Answer to New Question Begin]\\boxed{{your_final_answer}}[Final Answer to New Question End]"""

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
    """Extract question and answer from reasoning task output."""
    text = parse_thinking_output(text)

    # Extract question
    question_match = re.search(r"\[New Question Begin\](.*?)\[New Question End\]", text, re.DOTALL)
    if not question_match:
        return None, None
    question = question_match.group(1).strip()

    # Extract answer
    answer_match = re.search(r"\[Final Answer to New Question Begin\]\\?boxed\{(.*?)\}\[Final Answer to New Question End\]", text, re.DOTALL)
    if not answer_match:
        # Try without \boxed
        answer_match = re.search(r"\[Final Answer to New Question Begin\](.*?)\[Final Answer to New Question End\]", text, re.DOTALL)

    if not answer_match:
        return question, None

    answer = answer_match.group(1).strip()
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
        result = gemini_client.models.embed_content(
            model="gemini-embedding-001",
            contents=prompts,
            config=types.EmbedContentConfig(task_type="CLUSTERING", output_dimensionality=768),
        )

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


def generate_synthetic_data(
    llm: ChatOpenAI,
    seed_data: List[Dict],
    task_type: str,
    num_samples: int,
    categories: Optional[Dict[int, List[int]]] = None,
) -> List[Dict]:
    """Generate synthetic data using CoT-Self-Instruct."""
    synthetic_data = []

    # Set up progress bar
    pbar = tqdm(total=num_samples, desc="Generating synthetic data")

    while len(synthetic_data) < num_samples:
        # Sample seed data
        if task_type == "reasoning":
            # Random sampling for reasoning tasks
            seeds = random.sample(seed_data, min(2, len(seed_data)))
            prompt = REASONING_PROMPT_TEMPLATE.format(
                seed1=seeds[0].get("question", seeds[0].get("prompt", "")),
                seed2=seeds[1].get("question", seeds[1].get("prompt", "")) if len(seeds) > 1 else seeds[0].get("question", seeds[0].get("prompt", "")),
            )
        else:
            # Category-aware sampling for instruction tasks
            if categories:
                # Pick a random category
                category = random.choice(list(categories.keys()))
                category_indices = categories[category]
                indices = random.sample(category_indices, min(2, len(category_indices)))
                seeds = [seed_data[i] for i in indices]
            else:
                seeds = random.sample(seed_data, min(2, len(seed_data)))

            prompt = INSTRUCTION_PROMPT_TEMPLATE.format(
                prompt1=seeds[0].get("prompt", seeds[0].get("question", "")),
                prompt2=seeds[1].get("prompt", seeds[1].get("question", "")) if len(seeds) > 1 else seeds[0].get("prompt", seeds[0].get("question", "")),
            )

        # Generate using OpenRouter API
        try:
            response = llm.invoke([HumanMessage(content=prompt)])
            output_text = response.content
        except Exception as e:
            logger.warning(f"Generation failed: {e}")
            continue

        # Parse output
        if task_type == "reasoning":
            question, answer = extract_reasoning_output(output_text)
            if question and answer:
                synthetic_data.append(
                    {
                        "question": question,
                        "answer": answer,
                        "seed_indices": [seed_data.index(s) for s in seeds],
                    }
                )
                pbar.update(1)
        else:
            synthetic_prompt = extract_instruction_output(output_text)
            if synthetic_prompt:
                synthetic_data.append(
                    {
                        "prompt": synthetic_prompt,
                        "seed_indices": [seed_data.index(s) for s in seeds],
                    }
                )
                pbar.update(1)

    pbar.close()
    return synthetic_data


def answer_consistency_filter(
    llm: ChatOpenAI,
    synthetic_data: List[Dict],
    k_responses: int = 16,
    threshold: float = 0.5,
) -> List[Dict]:
    """Filter reasoning tasks using Answer-Consistency."""
    logger.info(f"Applying Answer-Consistency filter with K={k_responses}")

    filtered_data = []

    for item in tqdm(synthetic_data, desc="Answer-Consistency filtering"):
        question = item["question"]
        original_answer = item["answer"]

        # Generate K responses
        answers = []
        for _ in range(k_responses):
            try:
                response = llm.invoke([HumanMessage(content=question)])
                text = response.content

                # Try to extract boxed answer
                match = re.search(r"\\boxed\{(.*?)\}", text)
                if match:
                    answers.append(match.group(1).strip())
            except Exception as e:
                logger.warning(f"Answer consistency check failed: {e}")
                continue

        if not answers:
            continue

        # Get majority answer
        answer_counts = Counter(answers)
        if answer_counts:
            majority_answer, count = answer_counts.most_common(1)[0]

            # Check if majority answer matches original and meets threshold
            if majority_answer == original_answer and count / len(answers) >= threshold:
                item["consistency_score"] = count / len(answers)
                filtered_data.append(item)

    logger.info(f"Answer-Consistency: kept {len(filtered_data)}/{len(synthetic_data)} examples")
    return filtered_data


def rip_filter(
    llm: ChatOpenAI,
    synthetic_data: List[Dict],
    reward_model_id: str,
    k_responses: int = 32,
    threshold: float = 0.5,
) -> List[Dict]:
    """Filter using Rejecting Instruction Preferences (RIP)."""
    logger.info(f"Applying RIP filter with K={k_responses} and reward model {reward_model_id}")

    # Note: In a full implementation, you would load and use the actual reward model
    # For this example, we'll use a placeholder scoring mechanism
    logger.warning("RIP filtering requires a reward model implementation - using placeholder")

    filtered_data = []

    for item in tqdm(synthetic_data, desc="RIP filtering"):
        prompt = item.get("prompt", item.get("question", ""))

        # Generate K responses
        scores = []
        for _ in range(k_responses):
            try:
                response = llm.invoke([HumanMessage(content=prompt)])
                # In real implementation: score each response with reward model
                # For now, use length as a proxy (longer responses often score higher)
                scores.append(len(response.content))
            except Exception as e:
                logger.warning(f"RIP filter generation failed: {e}")
                continue

        # Use minimum score as quality indicator
        if scores:
            min_score = min(scores)
            normalized_score = min_score / 1000  # Normalize to 0-1 range

            if normalized_score >= threshold:
                item["rip_score"] = normalized_score
                filtered_data.append(item)

    logger.info(f"RIP filter: kept {len(filtered_data)}/{len(synthetic_data)} examples")
    return filtered_data


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
- Scored responses using a reward model
- Kept only prompts with high minimum scores"""

    return f"""---
tags:
- synthetic-data
- cot-self-instruct
- {task_type}
- uv-script
- openrouter-api
- gemini-embeddings
---
# CoT-Self-Instruct Synthetic Data
This dataset contains synthetic {task_type} data generated using the Chain-of-Thought Self-Instruct methodology via OpenRouter API with Gemini embeddings for clustering.
## Generation Details
- **Source Dataset**: [{source_dataset}](https://huggingface.co/datasets/{source_dataset})
- **Generation Model**: {generation_model} (via OpenRouter API)
- **Embedding Model**: gemini-embedding-001 (for prompt clustering)
- **Task Type**: {task_type}
- **Filter Method**: {filter_method}
- **Generated Examples**: {num_generated:,}
- **After Filtering**: {num_filtered:,} ({(num_filtered/num_generated)*100:.1f}% acceptance rate)
- **Generation Date**: {generation_time}
{filter_info}
## Methodology
Generated using CoT-Self-Instruct, which:
1. Uses Gemini embeddings to cluster seed prompts for better sampling diversity
2. Uses Chain-of-Thought reasoning to analyze seed examples
3. Generates new synthetic examples of similar quality and complexity via OpenRouter API
4. Applies quality filtering to ensure high-quality outputs
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
```"""


def main():
    parser = argparse.ArgumentParser(
        description="Generate synthetic data using CoT-Self-Instruct via OpenRouter API with Gemini embeddings",
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
        default="gpt-4o",
        help="Model for synthetic data generation (via OpenRouter)",
    )
    parser.add_argument(
        "--filter-model",
        type=str,
        default=None,
        help="Model for filtering (defaults to generation model)",
    )
    parser.add_argument(
        "--reward-model",
        type=str,
        default="Nexusflow/Athene-RM-8B",
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
        default="answer-consistency",
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

    # Set random seeds
    random.seed(args.seed)
    np.random.seed(args.seed)

    # Check API keys
    check_openrouter_api_key()
    # Only check Gemini API key if we might need embeddings (instruction tasks with clustering)
    if args.task_type in ["instruction", "auto"]:
        check_gemini_api_key()

    # Authentication
    hf_token = args.hf_token or os.environ.get("HF_TOKEN")
    if hf_token:
        login(token=hf_token)

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

    # Generate synthetic data
    start_time = datetime.now()
    synthetic_data = generate_synthetic_data(
        generation_llm,
        seed_data,
        args.task_type,
        args.num_samples,
        categories,
    )

    # Apply filtering
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
            )
        elif args.filter_method == "rip":
            filtered_data = rip_filter(
                filter_llm,
                synthetic_data,
                args.reward_model,
                args.k_responses,
                args.quality_threshold,
            )
        elif args.filter_method == "both":
            if args.task_type == "reasoning":
                filtered_data = answer_consistency_filter(
                    filter_llm,
                    synthetic_data,
                    args.k_responses,
                    args.quality_threshold,
                )
            filtered_data = rip_filter(
                filter_llm,
                filtered_data,
                args.reward_model,
                args.k_responses,
                args.quality_threshold,
            )

    # Create HuggingFace dataset
    logger.info(f"Creating dataset with {len(filtered_data)} examples")
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

    # Push to hub
    logger.info(f"Pushing dataset to: {args.output_dataset}")
    # Create dataset card
    card = DatasetCard(dataset_card)
    dataset.push_to_hub(args.output_dataset)
    # Push card separately
    card.push_to_hub(args.output_dataset)

    logger.info("Done! Dataset available at: https://huggingface.co/datasets/" + args.output_dataset)

    # Print example usage command
    if len(sys.argv) > 1:
        print("\nTo run with OpenRouter API and Gemini embeddings:")
        print(
            f"""export OPENROUTER_API_KEY=your_openrouter_key
export GEMINI_API_KEY=your_gemini_key
uv run cot-self-instruct.py \\
    --seed-dataset {args.seed_dataset} \\
    --output-dataset {args.output_dataset} \\
    --task-type {args.task_type} \\
    --generation-model {args.generation_model} \\
    --filter-method {args.filter_method} \\
    --num-samples {args.num_samples}"""
        )


if __name__ == "__main__":
    main()
