import * as Dialog from "@radix-ui/react-dialog";
import type { ReactNode } from "react";

interface ModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  children: ReactNode;
  footer: ReactNode;
}

/** A centred dialog for short decisions; focus returns to the opener on close. */
export function Modal({ open, onOpenChange, title, description, children, footer }: ModalProps) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="drawer-overlay" />
        <Dialog.Content className="modal">
          <Dialog.Title className="drawer-title">{title}</Dialog.Title>
          <Dialog.Description className={description ? "drawer-description" : "visually-hidden"}>{description ?? title}</Dialog.Description>
          <div className="modal-body">{children}</div>
          <footer className="modal-footer">{footer}</footer>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
