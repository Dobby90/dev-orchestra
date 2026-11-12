import type { ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
export function Modal({
  open,
  onOpenChange,
  title,
  description,
  children,
  trigger,
  footer,
}: {
  open: boolean;
  onOpenChange: (value: boolean) => void;
  title: string;
  description: string;
  children: ReactNode;
  trigger?: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      {trigger && <Dialog.Trigger asChild>{trigger}</Dialog.Trigger>}
      <Dialog.Portal>
        <Dialog.Overlay className="modal-overlay" />
        <Dialog.Content
          className={`modal${footer ? " modal-with-footer" : ""}`}
        >
          <Dialog.Title>{title}</Dialog.Title>
          <Dialog.Description>{description}</Dialog.Description>
          {footer ? (
            <>
              <div className="modal-scroll-body">{children}</div>
              <div className="modal-fixed-footer">{footer}</div>
            </>
          ) : (
            children
          )}
          <Dialog.Close
            className="icon-button modal-close"
            aria-label="대화상자 닫기"
          >
            <X size={18} />
          </Dialog.Close>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
