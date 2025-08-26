import json
import os
import subprocess
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv

load_dotenv()


def get_GENERATION_MODEL_from_config():
    """
    Reads the model name from config.yaml file.
    """
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    return config["api-endpoint"]["model"]


def _run_command(command: list[str], description: str):
    """Helper function to run a subprocess command and print its description."""
    print(description)
    subprocess.run(command, check=True)


def save_cot_as_jsonl(generated_dir: Path, final_dir: Path, file_stem: str):
    """
    Extracts cot_examples from the generated JSON file and saves them as a JSONL file,
    preserving the reasoning field.
    """
    generated_file = generated_dir / f"{file_stem}_cot_examples.json"
    final_file = final_dir / f"{file_stem}_cot_examples.jsonl"

    if not generated_file.exists():
        print(f"Warning: Generated file not found at {generated_file}. Skipping save for this document.")
        return

    print(f"Extracting CoT examples from {generated_file} and saving to {final_file}...")

    with open(generated_file, "r", encoding="utf-8") as f_in:
        data = json.load(f_in)

    cot_examples = data.get("cot_examples")

    if not cot_examples:
        print(f"Warning: No 'cot_examples' found in {generated_file}. Nothing to save.")
        return

    with open(final_file, "w", encoding="utf-8") as f_out:
        for example in cot_examples:
            json.dump(example, f_out, indent=4)
            f_out.write("\n")

    print(f"Successfully saved {len(cot_examples)} CoT examples to {final_file}.")


def run_pipeline_single_file(GENERATION_MODEL: str, file_path: Path, GENERATION_TYPE: Literal["qa", "cot", "summary"], PAIRS_PER_PAGE: int, CHARS_PER_PAGE: int):
    """
    Runs the synthetic data kit pipeline for a single document.
    """
    if not file_path.is_file():
        print(f"Error: File not found at {file_path}")
        return

    base_data_dir = Path(f"data_{GENERATION_MODEL.split('/')[-1]}")

    # Step 1: Create directory structure
    _run_command(
        ["mkdir", "-p", f"{base_data_dir}/input", f"{base_data_dir}/parsed", f"{base_data_dir}/generated", f"{base_data_dir}/curated", f"{base_data_dir}/final"],
        f"Creating directory structure: {base_data_dir}/{{input,parsed,generated,curated,final}}",
    )

    # Step 2: Ingest document and create QA pairs
    print(f"Processing document: {file_path.name}")

    # Ingest document
    parsed_output_dir = base_data_dir / "parsed"
    _run_command(
        ["synthetic-data-kit", "-c", "config.yaml", "ingest", str(file_path), "--output-dir", str(parsed_output_dir)],
        f"Ingesting {file_path.name}...",
    )

    # Calculate number of generations
    parsed_file_path = parsed_output_dir / f"{file_path.stem}.txt"
    if parsed_file_path.exists():
        with open(parsed_file_path, "r") as f:
            content = f.read()
        char_count = len(content)
        num_generations = max(1, (char_count // CHARS_PER_PAGE) * PAIRS_PER_PAGE)
        print(f"Calculated {num_generations} generations for {file_path.name} (character count: {char_count}).")

        # Create generations
        generated_output_dir = base_data_dir / "generated"
        _run_command(
            ["synthetic-data-kit", "-c", "config.yaml", "create", str(parsed_file_path), "--type", GENERATION_TYPE, "--num-pairs", str(num_generations), "--output-dir", str(generated_output_dir)],
            f"Creating {num_generations} generations for {file_path.name}...",
        )
    else:
        print(f"Warning: Parsed file not found for {file_path.name} at {parsed_file_path}. Skipping generation for this document.")

    # If generating CoT, skip the curation step as it's not supported.
    if GENERATION_TYPE == "cot":
        print("Skipping curation for CoT generation as it is not supported.")
        # Step 4: Manually extract CoT examples and save as JSONL to preserve reasoning.
        save_cot_as_jsonl(generated_dir=base_data_dir / "generated", final_dir=base_data_dir / "final", file_stem=file_path.stem)
    else:
        # Step 3: Curate data (for QA pairs)
        _run_command(
            ["synthetic-data-kit", "-c", "config.yaml", "curate", f"{base_data_dir}/generated/", "--model", GENERATION_MODEL, "--output", f"{base_data_dir}/curated", "--verbose"],
            "Curating data...",
        )

        # Step 4: Save as fine-tuning format (operates on the curated directory)
        _run_command(
            ["synthetic-data-kit", "-c", "config.yaml", "save-as", f"{base_data_dir}/curated/", "-f", "jsonl", "--storage", "json", "--output", f"{base_data_dir}/final"],
            "Saving data in fine-tuning format...",
        )

    print("Synthetic data generation pipeline completed for single file.")


if __name__ == "__main__":
    FILE_PATH = "documents/mckinsey-on-finance-number-80.pdf"

    PAIRS_PER_PAGE = 5
    CHARS_PER_PAGE = 3000
    GENERATION_TYPE = "cot"

    GENERATION_MODEL = get_GENERATION_MODEL_from_config()
    print(f"Using model from config.yaml: {GENERATION_MODEL}")

    run_pipeline_single_file(GENERATION_MODEL, Path(FILE_PATH), GENERATION_TYPE, PAIRS_PER_PAGE, CHARS_PER_PAGE)
