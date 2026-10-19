/**
 * The file types an invoice upload accepts, as the `accept` attribute of every
 * invoice file picker (the /invoices upload button and empty state, the create
 * modal, and the invoice modal's replace-file).
 *
 * The server's list is `ALLOWED_CONTENT_TYPES` in `backend/app/services/storage.py`;
 * `backend/tests/test_invoice_upload_types.py` fails when the two disagree. They
 * did: the pickers listed PDF and images only, so a structured e-invoice — a UBL
 * or CII `.xml`, which the server parses deterministically with no AI key
 * (`backend/docs/e-invoicing.md`) — could not be chosen in the file dialog at all,
 * and the one extraction path that works on a fresh production install with no
 * provider key was unreachable from the UI (#575).
 */
export const INVOICE_FILE_ACCEPT = '.pdf,.png,.jpg,.jpeg,.tiff,.xml';
