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


def update_config_yaml(GENERATION_MODEL: str):
    """
    Dynamically updates the config.yaml file with the provided API key and model name.
    """
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    config["api-endpoint"]["api_key"] = os.getenv("OPENROUTER_API_KEY")
    config["api-endpoint"]["model"] = GENERATION_MODEL
    config["generation"]["batch_size"] = os.cpu_count()
    config["curate"]["batch_size"] = os.cpu_count()
    config["curate"]["inference_batch"] = os.cpu_count()
    config["curate"]["threshold"] = 8.0

    with open("config.yaml", "w") as f:
        yaml.safe_dump(config, f, indent=2)


def _run_command(command: list[str], description: str):
    """Helper function to run a subprocess command and print its description."""
    print(description)
    subprocess.run(command, check=True)


def run_pipeline_single_file(GENERATION_MODEL: str, file_path: Path, PAIRS_PER_PAGE: int):
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

    # Calculate number of QA pairs
    parsed_file_path = parsed_output_dir / f"{file_path.stem}.txt"
    if parsed_file_path.exists():
        with open(parsed_file_path, "r") as f:
            content = f.read()
        char_count = len(content)
        num_qa_pairs = max(1, (char_count // CHARS_PER_PAGE) * PAIRS_PER_PAGE)  # At least 1 pair, 10 per 2000 chars
        print(f"Calculated {num_qa_pairs} QA pairs for {file_path.name} (character count: {char_count}).")

        # Create QA pairs
        generated_output_dir = base_data_dir / "generated"
        _run_command(
            ["synthetic-data-kit", "-c", "config.yaml", "create", str(parsed_file_path), "--type", "qa", "--num-pairs", str(num_qa_pairs), "--output-dir", str(generated_output_dir)],
            f"Creating {num_qa_pairs} QA pairs for {file_path.name}...",
        )
    else:
        print(f"Warning: Parsed file not found for {file_path.name} at {parsed_file_path}. Skipping QA pair generation for this document.")

    # Step 3: Curate data (now operates on the generated directory)
    _run_command(
        ["synthetic-data-kit", "-c", "config.yaml", "curate", f"{base_data_dir}/generated/", "--model", GENERATION_MODEL, "--output", f"{base_data_dir}/curated", "--verbose"],
        "Curating data...",
    )

    # Step 4: Save as fine-tuning format (now operates on the curated directory)
    _run_command(
        ["synthetic-data-kit", "-c", "config.yaml", "save-as", f"{base_data_dir}/curated/", "-f", "jsonl", "--storage", "json", "--output", f"{base_data_dir}/final"],
        "Saving data in fine-tuning format...",
    )

    print("Synthetic data generation pipeline completed for single file.")


if __name__ == "__main__":
    FILE_PATH = "documents/mckinsey-on-finance-number-80.pdf"

    GENERATION_MODEL = "google/gemini-2.5-flash"
    JUDGE_MODEL = "google/gemini-2.5-flash"
    PAIRS_PER_PAGE = 5
    CHARS_PER_PAGE = 3000

    print(f"Updating config.yaml with API key and model name...")
    update_config_yaml(GENERATION_MODEL)
    print(f"config.yaml updated successfully.")

    run_pipeline_single_file(GENERATION_MODEL, Path(FILE_PATH), PAIRS_PER_PAGE)
