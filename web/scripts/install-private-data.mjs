import fs from "node:fs";
import path from "node:path";

const webRoot = path.resolve(import.meta.dirname, "..");
const privateFile = path.resolve(
  process.env.COLLECTOR_DEMO_DATA_PATH ??
    path.join(webRoot, ".private/demo-creators.json"),
);
const targetFile = path.join(webRoot, "app/demo-creators.json");

if (!fs.existsSync(privateFile)) {
  throw new Error(
    `Private deployment data missing: ${privateFile}. Extract the private data bundle first.`,
  );
}

const payload = JSON.parse(fs.readFileSync(privateFile, "utf8"));
if (!Array.isArray(payload.creators) || payload.creators.length === 0) {
  throw new Error("Private deployment data has no creators");
}

fs.copyFileSync(privateFile, targetFile);
console.log(`Installed ${payload.creators.length} private creators for this build`);
