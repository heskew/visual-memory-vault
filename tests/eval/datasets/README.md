# Evaluation Datasets

This directory contains evaluation datasets for testing agent behavior.

## Running Evaluations

### Default Dataset
```bash
# Generate traces using the default dataset
agents-cli eval generate
agents-cli eval grade
```

### Custom Dataset
```bash
# Generate traces for a custom dataset
agents-cli eval generate --dataset tests/eval/datasets/custom-dataset.json --output custom_traces/
agents-cli eval grade --metrics general_quality --traces custom_traces/
```

### Deployed Agent

By default, `eval generate` starts a local HTTP server to run your agent in, dispatches each case in parallel and then tears the server down. Pass `--url <base_url> --app-name <name>` to target an already-running or deployed agent instead.

```bash
agents-cli eval generate --url https://my-agent.run.app --app-name app
```

## Dataset Format

Each dataset file follows the Gemini Enterprise Agent Platform Evaluation
dataset format. An eval case may use **either** of two shapes — both are
valid input to `agents-cli eval generate`:

**Shape A — single-prompt case:**

```json
{
  "eval_cases": [
    {
      "eval_case_id": "unique_case_id",
      "prompt": {
        "role": "user",
        "parts": [{"text": "User message"}]
      }
    }
  ]
}
```

**Shape B — continued-conversation case (the "N+1" pattern):**
The case carries prior turns in `agent_data` and the last turn ends with a
user message; `eval generate` appends the next agent response.

```json
{
  "eval_cases": [
    {
      "eval_case_id": "unique_case_id",
      "agent_data": {
        "turns": [
          {
            "turn_index": 0,
            "events": [
              {"author": "user",  "content": {"role": "user",  "parts": [{"text": "First user message"}]}},
              {"author": "agent", "content": {"role": "model", "parts": [{"text": "First agent reply"}]}},
              {"author": "user",  "content": {"role": "user",  "parts": [{"text": "Follow-up user message"}]}}
            ]
          }
        ]
      }
    }
  ]
}
```

## Key Fields

- `eval_cases`: Array of evaluation cases.
- `eval_case_id`: Unique identifier for the evaluation case (optional).
- `prompt`: A single user message — Shape A.
- `agent_data.turns`: Prior conversation turns ending with a user message — Shape B.

## Creating Custom Datasets

You can create custom datasets in two ways:

1. **By Hand**: Copy `basic-dataset.json` as a template and manually add evaluation cases.
2. **Synthesize**: Use the synthetic dataset generation command to generate conversation scenarios:
   ```bash
   agents-cli eval dataset synthesize --count 10
   ```

## Discovering Metrics

You can discover available out-of-the-box evaluation metrics by running:

```bash
agents-cli eval metric list
```

## Beyond Generate and Grade

Once you have a baseline, the eval surface has a few more commands worth knowing about:

- `agents-cli eval compare BASE CAND` — diff two grade-results files (regression check).
- `agents-cli eval analyze RESULTS` — cluster failure modes from a grade-results file.
- `agents-cli eval optimize` — auto-tune your agent's prompts using eval data.

See the [Evaluation Guide](https://google.github.io/agents-cli/guide/evaluation/) for the full surface and metric reference.


## Multimodal Receipt Dataset

`receipts-dataset.json` verifies the agent's headline capability: reading a
receipt image and extracting structured fields. Each case carries a receipt
image inline (`inline_data`, base64 JPEG) plus a text instruction, and its
`reference` holds the ground-truth `merchant` / `amount` / `currency` / `date`.

It is generated deterministically from `../fixtures/generate_receipt_dataset.py`
(synthetic receipts with known fields), so it is self-contained and needs no
external image files. Regenerate after editing the specs:

```bash
uv run python tests/eval/fixtures/generate_receipt_dataset.py
```

Grade it with the dedicated config, which selects the deterministic
`receipt_field_accuracy` metric (fraction of the four fields extracted
correctly — no judge model needed):

```bash
agents-cli eval run \
  --dataset tests/eval/datasets/receipts-dataset.json \
  --config tests/eval/receipts_eval_config.yaml
```

A full local run invokes the agent, so it needs `GOOGLE_API_KEY` (or Vertex
ADC) and, for the store step, a reachable Flair instance (see the Quickstart).
The dataset shape and the metric are covered by offline unit tests
(`tests/unit/test_receipts_dataset.py`, `tests/unit/test_receipt_metric.py`).
