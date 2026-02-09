"""Project save/load operations."""


def save_project(state, path: str):
    """Save project state to JSON file."""
    with open(path, 'w') as f:
        f.write(state.to_json())
    state._project_path = path


def load_project(state, path: str, sf2_loader=None):
    """Load project state from JSON file.
    
    Args:
        state: AppState to load into
        path: Path to JSON file
        sf2_loader: Optional callable(path) to reload SF2 into engine.
                    Called with the sf2 path hint from the project file.
    
    Raises whatever json.loads or file I/O raises on bad input.
    """
    with open(path) as f:
        state.load_json(f.read())
    state._project_path = path

    # Try to reload SF2 if path hint exists
    if sf2_loader and hasattr(state, '_sf2_path_hint') and state._sf2_path_hint:
        try:
            sf2_loader(state._sf2_path_hint)
        except Exception:
            pass
