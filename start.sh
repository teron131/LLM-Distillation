mkdir -p data/{input,parsed,generated,curated,final}
synthetic-data-kit -c config.yaml ingest ./documents/ --output-dir data/parsed
synthetic-data-kit -c config.yaml create ./data/parsed/ --type cot --output-dir data/generated
synthetic-data-kit -c config.yaml curate ./data/generated/ --output data/curated
synthetic-data-kit -c config.yaml save-as ./data/curated/ -f jsonl --storage json --output data/final