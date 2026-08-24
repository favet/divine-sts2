# State hashing specification (draft, non-certifying)

Canonical bytes are UTF-8 JSON with recursively lexicographically sorted object keys, preserved array order, no insignificant whitespace, invariant numeric formatting, and no NaN or Infinity. The build fingerprint and schema version are mandatory fields. The state hash is lowercase SHA-256 of those bytes.

Serialization and legal-action enumeration must never read or advance an RNG. Unsupported runtime types or fields are errors; they are not stringified or omitted. This draft cannot be declared stable until differential traces establish the complete field inventory.
