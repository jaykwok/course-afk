import { mkdtemp, mkdir, writeFile, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve, dirname, basename } from "node:path";
export const PASSWORD_TIME = "9223372036854775807";
export async function seedProfile(directory: string) {
  await mkdir(directory, { recursive: true });
  await writeFile(
    join(directory, "Local State"),
    JSON.stringify({
      password_manager: {
        os_password_blank: false,
        os_password_last_changed: PASSWORD_TIME,
      },
    }),
    { flag: "wx" },
  );
  await mkdir(join(directory, "Default"));
  await writeFile(
    join(directory, "Default", "Preferences"),
    JSON.stringify({
      credentials_enable_service: false,
      credentials_enable_autosignin: false,
      profile: { password_manager_enabled: false },
    }),
    { flag: "wx" },
  );
}
export async function validateProfile(directory: string) {
  try {
    const local = JSON.parse(
      await readFile(join(directory, "Local State"), "utf8"),
    );
    const prefs = JSON.parse(
      await readFile(join(directory, "Default", "Preferences"), "utf8"),
    );
    if (
      local.password_manager?.os_password_blank !== false ||
      local.password_manager?.os_password_last_changed !== PASSWORD_TIME ||
      prefs.credentials_enable_service !== false ||
      prefs.credentials_enable_autosignin !== false ||
      prefs.profile?.password_manager_enabled !== false
    )
      throw new Error();
  } catch {
    throw new Error("浏览器安全缓存未就绪，已阻止启动");
  }
}
export async function createProfile() {
  const dir = await mkdtemp(join(tmpdir(), "course-afk-"));
  try {
    await seedProfile(dir);
    await validateProfile(dir);
    return dir;
  } catch (e) {
    await removeProfile(dir);
    throw e;
  }
}
export async function removeProfile(directory: string) {
  const path = resolve(directory);
  if (
    dirname(path) !== resolve(tmpdir()) ||
    !basename(path).startsWith("course-afk-")
  )
    throw new Error("拒绝清理非应用临时目录");
  await rm(path, {
    recursive: true,
    force: true,
    maxRetries: 3,
    retryDelay: 200,
  });
}
