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


def save_cot_as_jsonl(generated_dir: Path, final_dir: Path, file_stem: str, source_filename: str):
    """
    Extracts cot_examples from the generated JSON file and saves them as a JSONL file,
    preserving the reasoning field and adding source filename.
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
            # Add source filename to each example
            example["source"] = source_filename
            json.dump(example, f_out, indent=4)
            f_out.write("\n")

    print(f"Successfully saved {len(cot_examples)} CoT examples to {final_file}.")


def clean_input_duplicates(data_dir: Path = Path("./documents")):
    """Cleans duplicates from the data directory."""
    duplicate_files = [f for f in data_dir.iterdir() if f.is_file() and " (1).pdf" in f.name]
    for file in duplicate_files:
        print(f"Deleting duplicate file: {file.name}")
        file.unlink()


def run_pipeline(
    GENERATION_MODEL: str,
    documents_dir: str = "./documents/",
    PAIRS_PER_PAGE: int = 5,
    CHARS_PER_PAGE: int = 3000,
    GENERATION_TYPE: Literal["qa", "cot", "summary"] = "qa",
):
    """
    Runs the synthetic data kit pipeline based on the start.sh script.
    The data directory will be dynamic based on the model name.
    """
    # Clean input duplicates
    clean_input_duplicates(Path(documents_dir))

    base_data_dir = Path(f"data_{GENERATION_MODEL.split('/')[-1]}_{GENERATION_TYPE.upper()}")

    # Step 1: Create directory structure
    _run_command(
        ["mkdir", "-p", f"{base_data_dir}/input", f"{base_data_dir}/parsed", f"{base_data_dir}/generated", f"{base_data_dir}/curated", f"{base_data_dir}/final"],
        f"Creating directory structure: {base_data_dir}/{{input,parsed,generated,curated,final}}",
    )

    # Step 2: Ingest documents and create pairs individually
    documents_path = Path(documents_dir)
    for doc_file in documents_path.iterdir():
        if doc_file.is_file():
            print(f"Processing document: {doc_file.name}")

            # Ingest document
            parsed_output_dir = base_data_dir / "parsed"
            _run_command(
                ["synthetic-data-kit", "-c", "config.yaml", "ingest", str(doc_file), "--output-dir", str(parsed_output_dir)],
                f"Ingesting {doc_file.name}...",
            )

            # Calculate number of generations
            # Assuming ingested files are named doc_file.name.txt in parsed_output_dir
            # The synthetic-data-kit ingest command outputs .txt files for PDFs, etc.
            # Need to read the content of the parsed file to get character count
            parsed_file_path = parsed_output_dir / f"{doc_file.stem}.txt"
            if parsed_file_path.exists():
                with open(parsed_file_path, "r") as f:
                    content = f.read()
                char_count = len(content)
                num_generations = max(1, (char_count // CHARS_PER_PAGE) * PAIRS_PER_PAGE)
                print(f"Calculated {num_generations} generations for {doc_file.name} (character count: {char_count}).")

                # Create generations
                generated_output_dir = base_data_dir / "generated"
                _run_command(
                    ["synthetic-data-kit", "-c", "config.yaml", "create", str(parsed_file_path), "--type", GENERATION_TYPE, "--num-pairs", str(num_generations), "--output-dir", str(generated_output_dir)],
                    f"Creating {num_generations} generations for {doc_file.name}...",
                )

                # If generating CoT, immediately save using custom function (skip curation)
                if GENERATION_TYPE == "cot":
                    save_cot_as_jsonl(generated_dir=generated_output_dir, final_dir=base_data_dir / "final", file_stem=doc_file.stem, source_filename=doc_file.stem)

            # If generating CoT, immediately save using custom function (skip curation)
            if GENERATION_TYPE == "cot":
                save_cot_as_jsonl(generated_dir=generated_output_dir, final_dir=base_data_dir / "final", file_stem=doc_file.stem, source_filename=doc_file.stem)
            else:
                print(f"Warning: Parsed file not found for {doc_file.name} at {parsed_file_path}. Skipping generation for this document.")

    # Steps 3-4: Only run curation and save-as for non-CoT generation types
    if GENERATION_TYPE != "cot":
        # Step 3: Curate data (now operates on the generated directory)
        _run_command(
            ["synthetic-data-kit", "-c", "config.yaml", "curate", f"{base_data_dir}/generated/", "--model", GENERATION_MODEL, "--output", f"{base_data_dir}/curated"],
            "Curating data...",
        )

        # Step 4: Save as fine-tuning format (now operates on the curated directory)
        _run_command(
            ["synthetic-data-kit", "-c", "config.yaml", "save-as", f"{base_data_dir}/curated/", "-f", "jsonl", "--storage", "json", "--output", f"{base_data_dir}/final"],
            "Saving data in fine-tuning format...",
        )
    else:
        print("Skipped curation and standard save-as for CoT generation - used custom CoT extraction instead.")

    print("Synthetic data generation pipeline completed.")


if __name__ == "__main__":
    PAIRS_PER_PAGE = 5
    CHARS_PER_PAGE = 3000
    GENERATION_TYPE = "cot"

    # Get model name from config.yaml
    GENERATION_MODEL = get_GENERATION_MODEL_from_config()
    print(f"Using Generation Model from config.yaml: {GENERATION_MODEL}")

    # Run the pipeline
    run_pipeline(
        GENERATION_MODEL,
        PAIRS_PER_PAGE=PAIRS_PER_PAGE,
        CHARS_PER_PAGE=CHARS_PER_PAGE,
        GENERATION_TYPE=GENERATION_TYPE,
    )
