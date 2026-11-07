import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import Ajv2020 from "ajv/dist/2020.js";

async function readJson(path) {
  return JSON.parse(await readFile(new URL(path, import.meta.url), "utf8"));
}

test("event schema keeps protocol version pinned", async () => {
  const source = await readFile(
    new URL("../schemas/event-envelope.schema.json", import.meta.url),
    "utf8",
  );
  const schema = JSON.parse(source);
  assert.equal(schema.properties.schema_version.const, "1.0");
  assert.ok(schema.properties.type.enum.includes("client_action.requested"));
  assert.ok(schema.properties.type.enum.includes("run.cancelled"));
  assert.deepEqual(schema.properties.tenant_id.type, ["string", "null"]);
  assert.deepEqual(schema.properties.user_id.type, ["string", "null"]);
  assert.equal(schema.additionalProperties, false);
});

test("canonical fixtures satisfy the event schema consistently", async () => {
  const schema = await readJson("../schemas/event-envelope.schema.json");
  const validFixture = await readJson("../fixtures/event.valid.json");
  const invalidFixture = await readJson("../fixtures/event.invalid.json");
  const ajv = new Ajv2020({ validateFormats: false });
  const validate = ajv.compile(schema);

  assert.equal(validate(validFixture), true);
  assert.equal(validate(invalidFixture), false);
});
