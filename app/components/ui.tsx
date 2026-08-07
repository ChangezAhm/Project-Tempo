/* Shared chrome for the Tempo UI — one place for buttons, panels, badges and
   page furniture so every screen carries the same ink/ivory register. */

import Link from "next/link";
import type { ReactNode } from "react";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/* --- Buttons --------------------------------------------------------------- */

const BUTTON_VARIANTS = {
  primary:
    "bg-ink text-neutral-50 shadow-sm hover:bg-neutral-700 disabled:hover:bg-ink",
  secondary:
    "border border-neutral-300 bg-white text-neutral-700 hover:border-neutral-400 hover:bg-neutral-50",
  ghost: "text-neutral-500 hover:bg-neutral-100 hover:text-ink",
  danger: "bg-red-700 text-white hover:bg-red-600",
  positive: "bg-role-input text-white hover:opacity-90",
} as const;

const BUTTON_SIZES = {
  md: "px-4 py-2 text-sm",
  sm: "px-3 py-1.5 text-xs",
  xs: "px-2.5 py-1 text-[11px]",
} as const;

export type ButtonVariant = keyof typeof BUTTON_VARIANTS;

export function buttonCls(
  variant: ButtonVariant = "primary",
  size: keyof typeof BUTTON_SIZES = "sm"
): string {
  return cx(
    "inline-flex items-center justify-center gap-1.5 rounded-md font-medium transition",
    "disabled:cursor-not-allowed disabled:opacity-45",
    BUTTON_VARIANTS[variant],
    BUTTON_SIZES[size]
  );
}

export function Button({
  variant = "primary",
  size = "sm",
  className,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: keyof typeof BUTTON_SIZES;
}) {
  return <button className={cx(buttonCls(variant, size), className)} {...props} />;
}

export function LinkButton({
  variant = "secondary",
  size = "sm",
  className,
  href,
  children,
}: {
  variant?: ButtonVariant;
  size?: keyof typeof BUTTON_SIZES;
  className?: string;
  href: string;
  children: ReactNode;
}) {
  return (
    <Link href={href} className={cx(buttonCls(variant, size), className)}>
      {children}
    </Link>
  );
}

/* --- Panels & badges ------------------------------------------------------- */

export function Panel({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return <section className={cx("panel", className)}>{children}</section>;
}

const BADGE_TONES = {
  neutral: "bg-neutral-100 text-neutral-600",
  green: "bg-role-input-soft text-role-input",
  amber: "bg-role-calc-soft text-role-calc",
  blue: "bg-role-output-soft text-role-output",
  warn: "bg-amber-50 text-amber-700",
  danger: "bg-red-50 text-red-700",
} as const;

export function Badge({
  tone = "neutral",
  className,
  title,
  children,
}: {
  tone?: keyof typeof BADGE_TONES;
  className?: string;
  title?: string;
  children: ReactNode;
}) {
  return (
    <span
      title={title}
      className={cx(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium tracking-wide",
        BADGE_TONES[tone],
        className
      )}
    >
      {children}
    </span>
  );
}

/* --- Page furniture -------------------------------------------------------- */

export function BackLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link
      href={href}
      className="inline-flex items-center gap-1 text-sm text-neutral-400 transition hover:text-ink"
    >
      <span aria-hidden>←</span> {children}
    </Link>
  );
}

export function PageHeader({
  title,
  eyebrow,
  children,
}: {
  title: ReactNode;
  eyebrow?: ReactNode;
  children?: ReactNode; // right-aligned actions
}) {
  return (
    <header className="flex flex-wrap items-end gap-3">
      <div className="min-w-0">
        {eyebrow ? (
          <p className="mb-1 text-[11px] font-medium uppercase tracking-[0.16em] text-neutral-400">
            {eyebrow}
          </p>
        ) : null}
        <h1 className="text-[1.9rem] leading-tight">{title}</h1>
      </div>
      {children ? <div className="ml-auto flex flex-wrap items-center gap-2">{children}</div> : null}
    </header>
  );
}

export function SectionHeader({
  title,
  hint,
  children,
}: {
  title: ReactNode;
  hint?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <div className="min-w-0">
        <h2 className="text-lg">{title}</h2>
        {hint ? <p className="mt-0.5 text-xs text-neutral-500">{hint}</p> : null}
      </div>
      {children ? <div className="ml-auto flex flex-wrap items-center gap-2">{children}</div> : null}
    </div>
  );
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-md border border-red-100 bg-red-50 px-3 py-2 text-sm text-red-700">
      {children}
    </p>
  );
}

export function EmptyState({
  title,
  body,
  children,
}: {
  title: ReactNode;
  body?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-neutral-300 bg-white/60 px-6 py-16 text-center">
      <h2 className="text-lg">{title}</h2>
      {body ? <p className="mt-1.5 max-w-md text-sm text-neutral-500">{body}</p> : null}
      {children ? <div className="mt-5">{children}</div> : null}
    </div>
  );
}
