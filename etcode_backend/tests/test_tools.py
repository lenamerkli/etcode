"""The tool schemas that the backend advertises to the model."""
from etcode_backend.tools import BACKEND_TOOLS, tool_names, tools_for


def schema(name, websearch=True, images=True):
    return next(t["function"] for t in tools_for(websearch, images) if t["function"]["name"] == name)


def test_read_file_parameters_and_defaults_are_stable():
    # Clients validate their arguments against this schema. Changing a name or default is a breaking change.
    read_file = schema("read_file")
    properties = read_file["parameters"]["properties"]
    assert set(properties) == {"path", "start_line", "end_line", "max_chars", "start_char", "end_char"}
    assert {k: v.get("default") for k, v in properties.items()} == {
        "path": None, "start_line": 1, "end_line": 1000, "max_chars": 1_000_000, "start_char": 0, "end_char": 100_000}
    assert {k: v["type"] for k, v in properties.items()} == {
        "path": "string", "start_line": "integer", "end_line": "integer", "max_chars": "integer",
        "start_char": "integer", "end_char": "integer"}
    assert read_file["parameters"]["required"] == ["path"]


def test_read_file_description_states_the_reading_rules():
    description = schema("read_file")["description"]
    for phrase in ("whichever comes later", "whichever of end_line, end_char or max_chars is reached first",
                   "the first 1000 lines and the first 100000 characters", "set end_line and end_char explicitly",
                   "first_char, last_char, first_line, last_line", "before and after that part",
                   "unread_chars, unread_lines"):
        assert phrase in description, phrase


def test_read_file_parameter_descriptions_state_the_reading_rules():
    properties = schema("read_file")["parameters"]["properties"]
    assert "1-indexed" in properties["start_line"]["description"] and "start_char is used instead" in properties["start_line"]["description"]
    assert "inclusive" in properties["end_line"]["description"] and "end_char, whichever comes first" in properties["end_line"]["description"]
    assert "0-indexed" in properties["start_char"]["description"] and "start_line is used instead" in properties["start_char"]["description"]
    end_char = properties["end_char"]["description"]
    assert "absolute" in end_char and "exclusive" in end_char and "starting after character 100000" in end_char
    assert "counted from the start position" in properties["max_chars"]["description"]
    assert all(p["description"] for p in properties.values())


def test_client_tools_are_advertised():
    client_tools = {"bash", "interactive_terminal", "read_file", "write_to_file", "replace_in_file", "save_messages"}
    assert tool_names(tools_for(False, False)) == client_tools
    assert tool_names(tools_for(True, True)) == client_tools | {"websearch", "view_image", "interactive_gui"}
    assert schema("bash")["description"] == "Execute a bash shell command."
    assert set(schema("bash")["parameters"]["properties"]) == {"command", "timeout", "directory", "venv", "max_chars"}
    assert set(schema("replace_in_file")["parameters"]["properties"]) == {"path", "search", "replace", "read"}


def test_save_messages_has_one_required_directory_and_runs_on_the_client():
    assert "save_messages" not in BACKEND_TOOLS
    parameters = schema("save_messages", websearch=False, images=False)["parameters"]
    assert parameters["required"] == ["directory"]
    assert set(parameters["properties"]) == {"directory"}
    assert parameters["properties"]["directory"]["type"] == "string"
