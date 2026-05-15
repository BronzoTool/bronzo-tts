"""Async worker for bronzo-tts -- launched as subprocess by the main CLI.

Reads job params from a JSON file, runs generate_audio, updates state.
"""

import json
import os
import sys
from pathlib import Path


def main():
    params_path = Path(sys.argv[1])
    if not params_path.exists():
        print(f"ERROR: params file not found: {params_path}")
        sys.exit(1)

    params = json.loads(params_path.read_text(encoding="utf-8"))
    job_id = params["job_id"]
    kwargs = params.get("kwargs", {})

    state_path = params_path.parent / f"{job_id}.json"

    # Import the main module
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from bronzo_tts import generate_audio

    try:
        result = generate_audio(**kwargs)
        state = {
            "job_id": job_id,
            "status": "completed",
            "started_at": params.get("started_at"),
            "finished_at": __import__("datetime").datetime.now().isoformat(),
            "result": result if isinstance(result, str) else str(result),
            "error": None,
        }
    except SystemExit as e:
        state = {
            "job_id": job_id,
            "status": "failed",
            "started_at": params.get("started_at"),
            "finished_at": __import__("datetime").datetime.now().isoformat(),
            "result": None,
            "error": str(e),
        }
    except Exception as e:
        state = {
            "job_id": job_id,
            "status": "failed",
            "started_at": params.get("started_at"),
            "finished_at": __import__("datetime").datetime.now().isoformat(),
            "result": None,
            "error": f"{type(e).__name__}: {e}",
        }

    state_path.write_text(json.dumps(state), encoding="utf-8")


if __name__ == "__main__":
    main()
