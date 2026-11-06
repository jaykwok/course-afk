import test from "node:test";
import assert from "node:assert/strict";
import {
  examState,
  entryProblem,
  newSubmission,
} from "../src/platform/exam.ts";
import { sectionStates } from "../src/platform/learning.ts";
import {
  decodeDocument,
  previewUrl,
  filename,
  downloadFile,
} from "../src/platform/references.ts";
import { entry } from "../src/domain/identity.ts";
import { COURSE, EXAM } from "./helpers.ts";
test("exam state distinguishes unknown limits, unlimited attempts, scaled scores and grading", () => {
  assert.equal(examState({}).allowed, undefined);
  assert.equal(examState({ allowExamTimes: 0 }).allowed, 0);
  assert.equal(examState({ allowExamTimes: 3, examedTimes: 2 }).remaining, 1);
  assert.equal(
    examState({
      passScore: 3,
      paperClass: { totalScore: 500 },
      examRecord: { score: 400, isFinished: 1 },
    }).passed,
    true,
  );
  assert.equal(examState({ examRecord: { isFinished: 1 } }).pending, true);
  assert.ok(entryProblem({ id: "wrong" }, EXAM.split("/").at(-1)!));
  assert.ok(
    entryProblem(
      {
        id: "x",
        type: 1,
        paperClass: { id: "paper" },
        examRecord: {},
        isSetAccessSecret: 1,
      },
      "x",
    ),
  );
});
test("server section evidence rejects wrong resource, duplicate and wrong course records", () => {
  const task = entry(COURSE, "a"),
    section = { id: "s", resourceId: "r", sectionType: 5 },
    record = {
      courseId: task.businessId,
      sectionId: "s",
      resourceId: "r",
      finishStatus: 2,
    };
  assert.equal(
    sectionStates([record], task, [section]).get("s").finishStatus,
    2,
  );
  assert.throws(() => sectionStates([record, record], task, [section]));
  assert.throws(() =>
    sectionStates([{ ...record, courseId: "other" }], task, [section]),
  );
  assert.throws(() =>
    sectionStates([{ ...record, resourceId: "other" }], task, [section]),
  );
});
test("historic submitted record cannot confirm a new submission", () => {
  const state = examState({
    examedTimes: 2,
    examRecord: { id: "old", isFinished: 1, score: 0 },
  });
  assert.equal(newSubmission(state, { recordId: "old", used: 2 }), false);
  assert.equal(
    newSubmission(state, { recordId: "old", used: 2, submitted: true }),
    false,
  );
  assert.equal(
    newSubmission(state, { recordId: "old", used: 2, submitted: false }),
    true,
  );
  assert.equal(
    newSubmission({ ...state, recordId: "new" }, { recordId: "old", used: 2 }),
    true,
  );
  assert.equal(
    newSubmission({ ...state, used: 3 }, { recordId: "old", used: 2 }),
    true,
  );
});
test("downloads reject non-documents and outside hosts; names stay within output directory", () => {
  for (const url of [
    "https://zhixueyun.com.evil.test/a",
    "https://u:p@kc.zhixueyun.com/a",
    "http://kc.zhixueyun.com/a",
  ])
    assert.throws(() => previewUrl(url));
  assert.equal(previewUrl("/api/document").origin, "https://kc.zhixueyun.com");
  assert.ok(!/[<>:/\\]/.test(filename("../CON:foo")));
  assert.throws(() => decodeDocument(Buffer.from("<html>login</html>"), "pdf"));
  assert.equal(
    decodeDocument(
      Buffer.from(Buffer.from("%PDF-1.7").toString("base64")),
      "pdf",
    ).ext,
    "pdf",
  );
});
test("redirected downloads never forward platform Authorization to CDN", async () => {
  const calls: { url: string; auth: unknown }[] = [];
  const fetcher = (async (url: any, options: any) => {
    calls.push({ url: String(url), auth: options.headers.Authorization });
    return calls.length === 1
      ? new Response(null, {
          status: 302,
          headers: { Location: "https://cdn.zhixueyun.com/file.pdf" },
        })
      : new Response("%PDF-1.7");
  }) as typeof fetch;
  const value = await downloadFile(
    "https://kc.zhixueyun.com/api/file",
    "secret",
    new AbortController().signal,
    fetcher,
  );
  assert.equal(value.toString(), "%PDF-1.7");
  assert.equal(calls[0].auth, "secret");
  assert.equal(calls[1].auth, undefined);
});
