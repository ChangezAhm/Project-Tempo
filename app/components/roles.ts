/* Sheet-role taxonomy for the workbook anatomy view.

   The parser's per-sheet roles (input / calc / lookup / data_dump / cover /
   instructions / mixed) map onto the four-part taxonomy the anatomy view and
   dependency graph present: Input / Calculation / Output / Reference.
   "Output" is derived: a calc/mixed sheet that other sheets feed but which
   feeds nothing else is where the workbook's numbers land. */

export type RoleKey = "input" | "calc" | "output" | "reference";

export const ROLE_META: Record<
  RoleKey,
  { label: string; color: string; soft: string; text: string; bg: string }
> = {
  input: {
    label: "Input",
    color: "var(--color-role-input)",
    soft: "var(--color-role-input-soft)",
    text: "text-role-input",
    bg: "bg-role-input-soft",
  },
  calc: {
    label: "Calculation",
    color: "var(--color-role-calc)",
    soft: "var(--color-role-calc-soft)",
    text: "text-role-calc",
    bg: "bg-role-calc-soft",
  },
  output: {
    label: "Output",
    color: "var(--color-role-output)",
    soft: "var(--color-role-output-soft)",
    text: "text-role-output",
    bg: "bg-role-output-soft",
  },
  reference: {
    label: "Reference",
    color: "var(--color-role-ref)",
    soft: "var(--color-role-ref-soft)",
    text: "text-role-ref",
    bg: "bg-role-ref-soft",
  },
};

export const ROLE_ORDER: RoleKey[] = ["input", "calc", "output", "reference"];

export function mapRole(
  rawRole: string | null | undefined,
  flow: { hasOutgoing: boolean; hasIncoming: boolean }
): RoleKey {
  switch (rawRole) {
    case "input":
      return "input";
    case "calc":
    case "mixed":
      return flow.hasIncoming && !flow.hasOutgoing ? "output" : "calc";
    case "lookup":
    case "data_dump":
    case "cover":
    case "instructions":
      return "reference";
    default:
      return flow.hasIncoming && !flow.hasOutgoing ? "output" : "calc";
  }
}
