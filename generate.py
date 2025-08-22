import os
import subprocess
from pathlib import Path

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


def clean_input_duplicates(data_dir: Path = Path("./documents")):
    """Cleans duplicates from the data directory."""
    duplicate_files = [f for f in data_dir.iterdir() if f.is_file() and " (1).pdf" in f.name]
    for file in duplicate_files:
        print(f"Deleting duplicate file: {file.name}")
        file.unlink()


def run_pipeline(
    GENERATION_MODEL: str,
    JUDGE_MODEL: str,
    documents_dir: str = "./documents/",
    PAIRS_PER_PAGE: int = 5,
    CHARS_PER_PAGE: int = 2000,
):
    """
    Runs the synthetic data kit pipeline based on the start.sh script.
    The data directory will be dynamic based on the model name.
    """
    # Clean input duplicates
    clean_input_duplicates(Path(documents_dir))

    base_data_dir = Path(f"data_{GENERATION_MODEL.split('/')[-1]}")

    # Step 1: Create directory structure
    _run_command(
        ["mkdir", "-p", f"{base_data_dir}/input", f"{base_data_dir}/parsed", f"{base_data_dir}/generated", f"{base_data_dir}/curated", f"{base_data_dir}/final"],
        f"Creating directory structure: {base_data_dir}/{{input,parsed,generated,curated,final}}",
    )

    # Step 2: Ingest documents and create QA pairs individually
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

            # Calculate number of QA pairs
            # Assuming ingested files are named doc_file.name.txt in parsed_output_dir
            # The synthetic-data-kit ingest command outputs .txt files for PDFs, etc.
            # Need to read the content of the parsed file to get character count
            parsed_file_path = parsed_output_dir / f"{doc_file.stem}.txt"
            if parsed_file_path.exists():
                with open(parsed_file_path, "r") as f:
                    content = f.read()
                char_count = len(content)
                num_qa_pairs = max(1, (char_count // CHARS_PER_PAGE) * PAIRS_PER_PAGE)  # At least 1 pair, 10 per 2000 chars
                print(f"Calculated {num_qa_pairs} QA pairs for {doc_file.name} (character count: {char_count}).")

                # Create QA pairs
                generated_output_dir = base_data_dir / "generated"
                _run_command(
                    ["synthetic-data-kit", "-c", "config.yaml", "create", str(parsed_file_path), "--type", "qa", "--num-pairs", str(num_qa_pairs), "--output-dir", str(generated_output_dir)],
                    f"Creating {num_qa_pairs} QA pairs for {doc_file.name}...",
                )
            else:
                print(f"Warning: Parsed file not found for {doc_file.name} at {parsed_file_path}. Skipping QA pair generation for this document.")

    # Step 3: Curate data (now operates on the generated directory)
    _run_command(
        ["synthetic-data-kit", "-c", "config.yaml", "curate", f"{base_data_dir}/generated/", "--model", JUDGE_MODEL, "--output", f"{base_data_dir}/curated"],
        "Curating data...",
    )

    # Step 4: Save as fine-tuning format (now operates on the curated directory)
    _run_command(
        ["synthetic-data-kit", "-c", "config.yaml", "save-as", f"{base_data_dir}/curated/", "-f", "jsonl", "--storage", "json", "--output", f"{base_data_dir}/final"],
        "Saving data in fine-tuning format...",
    )

    print("Synthetic data generation pipeline completed.")


if __name__ == "__main__":
    PAIRS_PER_PAGE = 5
    CHARS_PER_PAGE = 2000

    # Get model name from config.yaml
    GENERATION_MODEL = get_GENERATION_MODEL_from_config()
    JUDGE_MODEL = GENERATION_MODEL  # Assume judge model is the same as generation model
    print(f"Using Generation Model from config.yaml: {GENERATION_MODEL}")
    print(f"Using Judge Model: {JUDGE_MODEL}")

    # Run the pipeline
    run_pipeline(
        GENERATION_MODEL,
        JUDGE_MODEL,
        PAIRS_PER_PAGE=PAIRS_PER_PAGE,
        CHARS_PER_PAGE=CHARS_PER_PAGE,
    )
