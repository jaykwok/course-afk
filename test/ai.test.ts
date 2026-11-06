import { defaults } from "../src/config.ts";
import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import {
  AiClient,
  aiSettings,
  parseAnswer,
  validAnswers,
  questionProblem,
  type Question,
} from "../src/ai/client.ts";
const question: Question = {
  id: "q1",
  type: "single",
  text: "2+2?",
  options: [
    { label: "A", text: "4" },
    { label: "B", text: "5" },
  ],
  media: false,
  selector: "",
  optionSelector: ".options",
};
test("answer contract rejects duplicates, prose, unknown labels and incomplete/media questions", () => {
  for (const text of [
    "A",
    '```json\n{"status":"answered","answers":["A"]}\n```',
    '{"status":"manual","status":"answered","answers":["A"]}',
    '{"status":"answered","answers":["A","A"]}',
  ])
    assert.deepEqual(parseAnswer(text), []);
  assert.deepEqual(parseAnswer('{"answers":["A"],"status":"answered"}'), ["A"]);
  assert.equal(validAnswers(question, ["C"]), false);
  assert.equal(validAnswers(question, ["A", "B"]), false);
  assert.equal(validAnswers(question, ["A"]), true);
  assert.ok(questionProblem({ ...question, media: true }));
});
for (const protocol of ["chat", "responses"])
  test(`official SDK ${protocol} request and completed answer`, async () => {
    const requests: any[] = [];
    const server = createServer(async (req, res) => {
      let body = "";
      for await (const chunk of req) body += chunk;
      requests.push({
        path: req.url,
        body: JSON.parse(body),
        auth: req.headers.authorization,
      });
      res.setHeader("Content-Type", "application/json");
      res.end(
        JSON.stringify(
          protocol === "chat"
            ? {
                id: "x",
                choices: [
                  {
                    index: 0,
                    finish_reason: "stop",
                    message: {
                      role: "assistant",
                      content: '{"status":"answered","answers":["A"]}',
                    },
                  },
                ],
              }
            : {
                id: "x",
                object: "response",
                status: "completed",
                output: [
                  {
                    id: "m",
                    type: "message",
                    role: "assistant",
                    status: "completed",
                    content: [
                      {
                        type: "output_text",
                        text: '{"status":"answered","answers":["A"]}',
                        annotations: [],
                      },
                    ],
                  },
                ],
              },
        ),
      );
    });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
    const port = (server.address() as any).port;
    try {
      const settings = aiSettings({
        ...defaults().openai,
        baseURL: `http://127.0.0.1:${port}/v1`,
        apiKey: "test-key",
        model: "test",
        protocol: protocol as "chat" | "responses",
      });
      const client = new AiClient(settings);
      assert.deepEqual(
        await client.answer(question, new AbortController().signal),
        ["A"],
      );
      assert.equal(requests.length, 1);
      assert.equal(
        requests[0].path,
        protocol === "chat" ? "/v1/chat/completions" : "/v1/responses",
      );
      assert.equal(requests[0].body.enable_thinking, undefined);
      assert.equal(requests[0].body.enable_search, undefined);
      assert.equal(requests[0].auth, "Bearer test-key");
    } finally {
      server.closeAllConnections();
      await new Promise<void>((r) => server.close(() => r()));
    }
  });
test("SDK cancellation aborts in-flight requests and does not retry", async () => {
  let count = 0;
  const server = createServer((_req, _res) => {
    count++;
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  try {
    const client = new AiClient(
        aiSettings({
          ...defaults().openai,
          baseURL: `http://127.0.0.1:${(server.address() as any).port}/v1`,
          apiKey: "test-key",
          model: "test",
        }),
      ),
      abort = new AbortController();
    setTimeout(() => abort.abort(new Error("cancel")), 100);
    await assert.rejects(client.answer(question, abort.signal), /cancel/);
    assert.equal(count, 1);
  } finally {
    server.closeAllConnections();
    await new Promise<void>((r) => server.close(() => r()));
  }
});
