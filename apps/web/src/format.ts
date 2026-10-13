export const date = (value: string | null | undefined) =>
  value ? new Date(value).toLocaleString() : "Not captured";
