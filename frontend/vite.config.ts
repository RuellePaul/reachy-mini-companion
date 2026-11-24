/// <reference types="vitest/config" />
import {defineConfig, loadEnv} from 'vite';
import react from '@vitejs/plugin-react';
import {readFileSync, readdirSync} from 'fs';
import type {ClientRequest, IncomingMessage} from 'http';
import {isIP} from 'net';
import * as path from 'path';

const KEY_VAR = 'MAC_AGENT_API_KEY';
// Vite hands every VITE_ variable to client code (and inlines import.meta.env in dev): never the key's name.
const LEAKY_KEY_VAR = 'VITE_MAC_AGENT_API_KEY';

/**
 * Whether the request was addressed to this machine by a name nobody else controls: localhost,
 * `*.localhost` or an IP literal. Any other name is DNS rebinding — Vite refuses it on HTTP, but not
 * on a proxied WebSocket upgrade, so the check covers both.
 */
export const localHost = (host: string | undefined): boolean => {
    const trimmed = host?.trim().toLowerCase() ?? '';
    if (trimmed.startsWith('[')) {
        const end = trimmed.indexOf(']');
        return end > 0 && isIP(trimmed.slice(1, end)) === 6;
    }
    const hostname = trimmed.split(':')[0];
    return isIP(hostname) === 4 || hostname === 'localhost' || hostname.endsWith('.localhost');
};

/**
 * Whether a request comes from the dashboard's own pages: the dev server listens on the LAN, and any
 * site open in the browser can aim a request at it. Browsers send `Sec-Fetch-Site` to localhost but
 * not to a plain-HTTP LAN address (the phone), where `Origin` gives a foreign page away — it is sent
 * on every WebSocket upgrade and cross-origin POST, so an upgrade without one is not the dashboard's.
 */
export const fromDashboard = (req: IncomingMessage, upgrade: boolean): boolean => {
    if (!localHost(req.headers.host)) return false;
    const site = req.headers['sec-fetch-site'];
    if (site !== undefined && site !== 'same-origin' && site !== 'none') return false;
    const origin = req.headers.origin;
    if (origin === undefined) return !upgrade;
    try {
        return new URL(origin).host === req.headers.host?.toLowerCase();
    } catch {
        return false; // `Origin: null`: a sandboxed or file:// page
    }
};

/**
 * Routes only the robot calls (everything under /api/robot/): they never get the key here, so a
 * foreign page that passes `fromDashboard` (a no-cors GET to the LAN address carries neither header)
 * still cannot take the robot's commands. The path is normalised the way the Mac reads it.
 */
export const robotOnly = (url: string | undefined): boolean => {
    try {
        const decoded = decodeURIComponent((url ?? '').split(/[?#]/)[0]).replace(/\/{2,}/g, '/');
        const pathname = new URL(decoded, 'http://proxy.invalid').pathname.toLowerCase();
        return pathname === '/api/robot' || pathname.startsWith('/api/robot/');
    } catch {
        return true;
    }
};

/** The icons the dashboard's code imports, each by its own path (eslint refuses MUI's barrel). */
const icons = (): string[] => {
    const src = path.resolve(__dirname, 'src');
    const files = readdirSync(src, {recursive: true, encoding: 'utf8'}).filter((f) => /\.tsx?$/.test(f));
    const found = files.flatMap((f) => [
        ...readFileSync(path.join(src, f), 'utf8').matchAll(/'(@mui\/icons-material\/\w+)'/g)
    ]);
    return [...new Set(found.map((m) => m[1]))].sort();
};

export default defineConfig(({mode}) => {
    // The key lives in the repo-root .env next to the Mac's own copy; frontend/.env is the fallback.
    const rootEnv = loadEnv(mode, path.resolve(__dirname, '..'), '');
    const env = loadEnv(mode, __dirname, '');
    const apiKey = rootEnv[KEY_VAR] || env[KEY_VAR] || '';
    if (rootEnv[LEAKY_KEY_VAR] || env[LEAKY_KEY_VAR]) {
        console.warn(
            `\n\x1b[41m\x1b[97m ${LEAKY_KEY_VAR} is set \x1b[0m\x1b[31m — Vite exposes every VITE_ variable to the ` +
                'browser. It is ignored: remove it and set MAC_AGENT_API_KEY in the repo-root .env.\x1b[0m\n'
        );
    }

    // `proxyReq` does not fire on a WebSocket upgrade, hence `proxyReqWs` as well.
    const addKey = (proxyReq: ClientRequest, req: IncomingMessage, upgrade: boolean) => {
        if (apiKey && !robotOnly(req.url) && fromDashboard(req, upgrade)) proxyReq.setHeader('X-API-Key', apiKey);
    };

    return {
        plugins: [react()],
        // A test that renders the whole dashboard takes seconds on a loaded machine; one cut off at vitest's default
        // 5 s keeps running into the next test's page.
        test: {testTimeout: 30_000, setupFiles: ['tests/setup.ts']},
        // The studio's 3D and every icon, pre-bundled up front: a dependency first met at runtime (a surface opened
        // later) makes Vite reload the page. A new icon changes this list, so the next start bundles them again.
        optimizeDeps: {
            include: [
                'three',
                'three/addons/controls/OrbitControls.js',
                'three/addons/libs/meshopt_decoder.module.js',
                'three/addons/loaders/GLTFLoader.js',
                'three/addons/utils/BufferGeometryUtils.js',
                ...icons()
            ]
        },
        server: {
            host: '0.0.0.0',
            port: 5077,
            proxy: {
                '/api': {
                    target: env.VITE_MAC_AGENT_URL || 'http://localhost:7777',
                    changeOrigin: true,
                    ws: true,
                    // X-Forwarded-For: a phone on the LAN must not reach the Mac's loopback-only routes as 127.0.0.1.
                    xfwd: true,
                    configure: (proxy) => {
                        proxy.on('proxyReq', (proxyReq, req) => addKey(proxyReq, req, false));
                        proxy.on('proxyReqWs', (proxyReq, req) => addKey(proxyReq, req, true));
                    }
                }
            }
        }
    };
});
