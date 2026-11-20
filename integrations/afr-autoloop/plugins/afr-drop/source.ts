import { createHmac, timingSafeEqual } from "node:crypto";

type Input = { headers: Record<string, string | string[] | undefined>; body: string; secret: string };

export default function afrDrop({headers, body, secret}: Input) {
  const expected = Buffer.from(createHmac("sha256", secret).update(body).digest("hex"));
  const signature = headers["x-afr-signature"];
  if (typeof signature !== "string" || !secret || !/^[a-f0-9]{64}$/.test(signature) ||
      !timingSafeEqual(Buffer.from(signature), expected)) throw new Error("invalid signature");
  const data = JSON.parse(body);
  const {sha256, attempt, generation} = data ?? {};
  if (Object.keys(data ?? {}).sort().join(",") !== "attempt,generation,sha256" ||
      typeof sha256 !== "string" || !/^[a-f0-9]{64}$/.test(sha256) ||
      typeof generation !== "string" || !/^(initial|[a-f0-9]{16})$/.test(generation) ||
      !Number.isInteger(attempt) || attempt < 1 || attempt > 3) throw new Error("invalid AFR work reference");
  return [{id: `afr-drop:${sha256}:${generation}:${attempt}`, type: "afr.intake.pending",
           subject: sha256, detail: JSON.stringify({sha256, attempt, generation})}];
}
