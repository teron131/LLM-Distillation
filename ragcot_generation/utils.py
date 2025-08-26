import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)

# Configuration constants
SAVE_DIR = Path("./generated_data")


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
