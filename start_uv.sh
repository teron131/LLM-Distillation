uv run uvscripts_generate.py \
    --generation-model google/gemini-2.5-flash-lite \
    --seed-dataset OrcinusOrca/McKinsey-reports \
    --output-dataset Orc1nusOrca/synthetic-math \
    --task-type reasoning \
    --num-samples 10