// The values the site shows and the playground starts with. Everything derived from them, such
// as their bytes and hashes, is computed by Fe Codec itself (see values.ts), never written here.

/** The Order from examples/vanilla, as JSON. */
export const ORDER = `{
  "id": 7,
  "buyer": "0xd8da6bf26964af9d7eed9e03e53415d37aa96045",
  "total": 1000000000000000000,
  "status": "Paid",
  "note": "gift"
}`;

/** The EIP's example mail, as JSON. */
export const MAIL = `{
  "from": { "name": "Cow", "wallet": "0xcd2a3d9f938e13cd947ec05abc7fe734df8dd826" },
  "to": { "name": "Bob", "wallet": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" },
  "contents": "Hello, Bob!"
}`;
