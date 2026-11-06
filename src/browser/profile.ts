import {
  mkdtemp,
  mkdir,
  writeFile,
  readFile,
  rm,
  readdir,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve, dirname, basename } from "node:path";
import { atomicJson } from "../storage/files.ts";
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
      signin: { allowed_on_device: false },
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
      signin: { allowed: false, allowed_on_next_startup: false },
      session: { restore_on_startup: 5, startup_urls: [] },
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
      prefs.profile?.password_manager_enabled !== false ||
      prefs.signin?.allowed !== false ||
      prefs.signin?.allowed_on_next_startup !== false ||
      local.signin?.allowed_on_device !== false
    )
      throw new Error();
  } catch {
    throw new Error("浏览器安全缓存未就绪，已阻止启动");
  }
}
/** Call while holding the application's instance lock. Never adopt a personal profile. */
export async function prepareProjectProfile(directory: string) {
  await mkdir(directory, { recursive: true });
  const marker = join(directory, "course-afk-profile.json");
  const files = await readdir(directory);
  if (files.length === 0)
    await atomicJson(marker, { application: "course-afk", version: 1 });
  else {
    let owned;
    try {
      owned = JSON.parse(await readFile(marker, "utf8"));
    } catch {}
    if (owned?.application !== "course-afk" || owned.version !== 1)
      throw new Error(
        "浏览器目录不属于此项目，请使用空的 browser-profile 目录",
      );
  }
  const read = async (path: string) => {
    try {
      return JSON.parse(await readFile(path, "utf8"));
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code === "ENOENT") return {};
      throw e;
    }
  };
  const localPath = join(directory, "Local State"),
    prefsPath = join(directory, "Default", "Preferences");
  const local = await read(localPath),
    prefs = await read(prefsPath);
  // Reapply the password-probe cache before every launch, preserving all other state.
  local.password_manager = {
    ...local.password_manager,
    os_password_blank: false,
    os_password_last_changed: PASSWORD_TIME,
  };
  local.signin = { ...local.signin, allowed_on_device: false };
  prefs.credentials_enable_service = false;
  prefs.credentials_enable_autosignin = false;
  prefs.profile = { ...prefs.profile, password_manager_enabled: false };
  prefs.signin = {
    ...prefs.signin,
    allowed: false,
    allowed_on_next_startup: false,
  };
  prefs.session = { ...prefs.session, restore_on_startup: 5, startup_urls: [] };
  await atomicJson(localPath, local);
  await atomicJson(prefsPath, prefs);
  await validateProfile(directory);
  return directory;
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
