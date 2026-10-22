import os

import uvicorn


def main() -> None:
    # The workbench drain canonicalizes the frozen market for minutes.
    # Keep that CPU out of this process so /api/discoveries/current can answer.
    os.environ["BERKSHIRE_DISCOVERY_OFFLOAD"] = "1"
    uvicorn.run("app.main:app", host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
