from meta_coder.settings import RunSettings, load_run_settings, save_run_settings


class FakeProject:
    def __init__(self, tmp_path):
        self.path = tmp_path


def test_defaults_when_no_settings_file(tmp_path):
    settings = load_run_settings(FakeProject(tmp_path))
    assert settings.parallel_requests == 1
    assert settings.request_delay_sec == 0


def test_save_and_load_round_trip(tmp_path):
    project = FakeProject(tmp_path)
    save_run_settings(project, RunSettings(model="gemini-x", parallel_requests=5, request_delay_sec=10))
    reloaded = load_run_settings(project)
    assert reloaded.model == "gemini-x"
    assert reloaded.parallel_requests == 5
    assert reloaded.request_delay_sec == 10


def test_clamps_out_of_range_values(tmp_path):
    project = FakeProject(tmp_path)
    save_run_settings(project, RunSettings(parallel_requests=999, request_delay_sec=-5))
    reloaded = load_run_settings(project)
    assert reloaded.parallel_requests == 32
    assert reloaded.request_delay_sec == 0


def test_corrupt_settings_file_falls_back_to_defaults(tmp_path):
    project = FakeProject(tmp_path)
    settings_path = tmp_path / ".meta_coder" / "run_settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("not json", encoding="utf-8")
    settings = load_run_settings(project)
    assert settings.parallel_requests == 1


def test_unknown_provider_falls_back_to_default_provider_and_model():
    settings = RunSettings(provider="bogus").clamped()
    assert settings.provider == "gemini"
    assert settings.model  # a real default model string, not blank


def test_blank_model_falls_back_to_the_selected_providers_default():
    settings = RunSettings(provider="openrouter", model="").clamped()
    assert settings.provider == "openrouter"
    assert settings.model == "google/gemini-3.8-flash"


def test_provider_defaults_are_the_expected_gemini_models():
    assert RunSettings(provider="gemini", model="").clamped().model == "gemini-3.8-flash"
    assert RunSettings(provider="openrouter", model="").clamped().model == "google/gemini-3.8-flash"


def test_previously_saved_default_model_is_not_rewritten(tmp_path):
    # Changing DEFAULT_MODEL must not silently change a project's saved choice.
    project = FakeProject(tmp_path)
    save_run_settings(project, RunSettings(provider="gemini", model="gemini-3.7-flash"))
    assert load_run_settings(project).model == "gemini-3.7-flash"


def test_zero_timeout_falls_back_to_the_providers_default_timeout():
    gemini_settings = RunSettings(provider="gemini", request_timeout_sec=0).clamped()
    openrouter_settings = RunSettings(provider="openrouter", request_timeout_sec=0).clamped()
    assert gemini_settings.request_timeout_sec == 600
    assert openrouter_settings.request_timeout_sec == 600


def test_out_of_range_timeout_is_clamped():
    settings = RunSettings(request_timeout_sec=5).clamped()
    assert settings.request_timeout_sec == 30  # MIN_REQUEST_TIMEOUT_SEC
    settings = RunSettings(request_timeout_sec=999999).clamped()
    assert settings.request_timeout_sec == 3600  # MAX_REQUEST_TIMEOUT_SEC


def test_invalid_reasoning_effort_is_dropped_not_saved():
    settings = RunSettings(reasoning_effort="ultra-mega").clamped()
    assert settings.reasoning_effort == ""
    settings = RunSettings(reasoning_effort="HIGH").clamped()
    assert settings.reasoning_effort == "high"
