// Single source of truth for the app's displayed version + release channel.
//
// Bump APP_VERSION here whenever you bump web/package.json + pyproject.toml.
// Every UI surface (top header, sidebar, login, marketing home + footer) reads
// APP_VERSION_LABEL from this module, so the version string can never drift
// across the app again (it previously hard-coded "v0.1 dev preview" in six
// places while package.json had moved on).
export const APP_VERSION = "0.5.1";
export const APP_CHANNEL = "dev preview";

/** Standard short badge, e.g. "v0.4.0 dev preview". */
export const APP_VERSION_LABEL = `v${APP_VERSION} ${APP_CHANNEL}`;
