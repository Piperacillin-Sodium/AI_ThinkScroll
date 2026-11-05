"""Apply a saved creative language to one serial job, then restore module state."""
from contextlib import contextmanager


@contextmanager
def creative_language(config):
    import config_manager
    import prompt_definitions
    previous_flag = config_manager.IS_ENGLISH
    previous = {name: value for name, value in vars(prompt_definitions).items()
                if not name.startswith("__") and isinstance(value, str)}
    english = config.get("language", "zh") == "en"
    added = []
    config_manager.IS_ENGLISH = english
    try:
        if english:
            import prompt_definitions_en
            for name, value in vars(prompt_definitions_en).items():
                if not name.startswith("__") and isinstance(value, str):
                    if name not in previous:
                        added.append(name)
                    setattr(prompt_definitions, name, value)
        yield
    finally:
        for name, value in previous.items():
            setattr(prompt_definitions, name, value)
        for name in added:
            delattr(prompt_definitions, name)
        config_manager.IS_ENGLISH = previous_flag
