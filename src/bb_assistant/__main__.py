if __name__ == "__main__":
    try:
        from bb_assistant.app import main
        main()
    except Exception:
        import os
        import traceback
        from pathlib import Path

        error_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BBMarkAssistant"
        error_dir.mkdir(parents=True, exist_ok=True)
        (error_dir / "startup-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
