import { AlertTriangle, CheckCircle2, CircleAlert, Info } from "lucide-react";
import { alertClass, alertRole, type AlertSize, type AlertTone } from "@/lib/alerts";
import { cn } from "@/lib/utils";

const ICONS = { error: CircleAlert, warning: AlertTriangle, success: CheckCircle2, info: Info } as const;

/**
 * The one banner for errors, warnings, success and info notices.  Colours,
 * border, radius, spacing and icon come from the semantic tokens in
 * globals.css, so every alert looks the same in light and dark mode.
 */
export function Alert({ tone, title, children, size = "md", icon = true, role, className, action, id }: {
  tone: AlertTone;
  title?: React.ReactNode;
  children?: React.ReactNode;
  size?: AlertSize;
  icon?: boolean;
  role?: "alert" | "status" | "none";
  className?: string;
  action?: React.ReactNode;
  id?: string;
}) {
  const Icon = ICONS[tone];
  const resolvedRole = role === "none" ? undefined : role ?? alertRole(tone);
  return (
    <div id={id} role={resolvedRole} data-tone={tone} className={cn(alertClass(tone, size), className)}>
      {icon && <Icon className="cf-alert-icon" aria-hidden />}
      <div className="cf-alert-body">
        {title && <span className="cf-alert-title">{title}</span>}
        {children}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}
