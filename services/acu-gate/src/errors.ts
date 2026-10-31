export class GateError extends Error {
  status: number;
  code: string;
  extra: Record<string, unknown>;
  constructor(
    status: number,
    code: string,
    extra: Record<string, unknown> = {},
  ) {
    super(code.replaceAll("_", " "));
    this.status = status;
    this.code = code;
    this.extra = extra;
  }
}
/** object provides its exported service behavior. */
export function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new GateError(400, "invalid_request");
  return value as Record<string, unknown>;
}
/** text provides its exported service behavior. */
export function text(value: unknown, max = 4096): string {
  if (typeof value !== "string" || !value.length || value.length > max)
    throw new GateError(400, "invalid_request");
  return value;
}
