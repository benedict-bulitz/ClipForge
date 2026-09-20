import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 rounded-full text-sm font-semibold transition-[transform,background-color,border-color,color,box-shadow,opacity] duration-150 ease-[cubic-bezier(.23,1,.32,1)] active:scale-[.97] disabled:pointer-events-none disabled:opacity-50 disabled:active:scale-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#ff6838]/40",
  {
    variants: {
      variant: {
        default: "bg-[#171714] px-5 py-3 text-white hover:bg-black hover:shadow-lg",
        accent: "bg-[#ff6838] px-5 py-3 text-white shadow-[0_8px_24px_rgba(255,104,56,.24)] hover:bg-[#ed5728]",
        outline: "border border-black/10 bg-white/70 px-4 py-2.5 text-[#25251f] hover:bg-white dark:border-white/10 dark:bg-white/[.07] dark:text-[#efeee7] dark:hover:bg-white/[.11]",
        ghost: "px-3 py-2 text-[#69695f] hover:bg-black/5 hover:text-[#171714] dark:text-[#aaa99f] dark:hover:bg-white/[.07] dark:hover:text-white",
      },
      size: { default: "h-11", sm: "h-9 text-xs", lg: "h-14 px-7 text-base", icon: "size-10 p-0" },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> &
  VariantProps<typeof buttonVariants> & { asChild?: boolean };

export function Button({ className, variant, size, asChild, ...props }: ButtonProps) {
  const Comp = asChild ? Slot : "button";
  return <Comp className={cn(buttonVariants({ variant, size }), className)} {...props} />;
}
