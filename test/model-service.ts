import { createServer } from "node:http";
import type { AddressInfo } from "node:net";

export async function modelService() {
  const requests: { path: string; method: string; auth: string }[] = [];
  const state = {
    status: 200,
    models: ["orbit-reasoner", "atlas-mini", "atlas-chat"],
    hold: undefined as Promise<void> | undefined,
  };
  const server = createServer(async (req, res) => {
    requests.push({
      path: req.url!,
      method: req.method!,
      auth: req.headers.authorization!,
    });
    await state.hold;
    res.writeHead(state.status, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify(
        state.status === 200
          ? {
              object: "list",
              data: state.models.map((id) => ({
                id,
                object: "model",
                created: 0,
                owned_by: "fixture",
              })),
            }
          : {
              error: {
                message: `Provider echoed ${req.headers.authorization}`,
              },
            },
      ),
    );
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  return {
    baseURL: `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1`,
    state,
    requests,
    close: () =>
      new Promise<void>((resolve, reject) => {
        server.close((error) => (error ? reject(error) : resolve()));
        server.closeAllConnections();
      }),
  };
}
