import { Slot } from "@radix-ui/react-slot";
import type { ButtonHTMLAttributes, ReactNode } from "react";

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  asChild?: boolean;
  children: ReactNode;
};

export function Button({ asChild, className = "", children, ...props }: ButtonProps) {
  const Comp = asChild ? Slot : "button";
  return (
    <Comp
      className={`inline-flex h-9 items-center justify-center gap-2 rounded-md border border-line bg-paper px-3 text-sm font-medium text-ink shadow-sm transition hover:bg-field ${className}`}
      {...props}
    >
      {children}
    </Comp>
  );
}
