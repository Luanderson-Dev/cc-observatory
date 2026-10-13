"""JSON value types shared by the capture tools."""

type JSON = str | int | float | bool | list[JSON] | dict[str, JSON] | None
type JSONObject = dict[str, JSON]
