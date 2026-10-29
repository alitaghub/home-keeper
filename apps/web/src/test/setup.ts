import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom has no modal dialogs: opening sets `open`, closing clears it and fires `close`.
HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
  this.open = true;
};
HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
  this.open = false;
  this.dispatchEvent(new Event("close"));
};

afterEach(() => {
  cleanup();
});
