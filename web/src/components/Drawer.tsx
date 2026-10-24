import * as Dialog from "@radix-ui/react-dialog";
import { useEffect, useRef, type ReactNode } from "react";

interface DrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
}

/** A side panel for secondary work. Focus moves in and returns to the opener on close. */
export function Drawer({ open, onOpenChange, title, description, children, footer }: DrawerProps) {
  // Remember whatever opened the drawer so closing it returns focus there.
  const opener = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (open && document.activeElement instanceof HTMLElement) opener.current = document.activeElement;
  }, [open]);
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="drawer-overlay" />
        <Dialog.Content
          className="drawer"
          onCloseAutoFocus={event => {
            if (opener.current?.isConnected) {
              event.preventDefault();
              opener.current.focus();
            }
          }}
        >
          <header className="drawer-header">
            <div>
              <Dialog.Title className="drawer-title">{title}</Dialog.Title>
              {description ? <Dialog.Description className="drawer-description">{description}</Dialog.Description> : <Dialog.Description className="visually-hidden">{title}</Dialog.Description>}
            </div>
            <Dialog.Close className="button icon-button" data-variant="ghost" aria-label={`Close ${title}`}>
              <span aria-hidden="true">✕</span>
            </Dialog.Close>
          </header>
          <div className="drawer-body">{children}</div>
          {footer ? <footer className="drawer-footer">{footer}</footer> : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
