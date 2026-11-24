const fs = require('fs');
const path = require('path');

const frontendDir = path.join(__dirname, 'frontend');
const bin = (name) => path.join(frontendDir, 'node_modules', '.bin', name);
const venvRuff = path.join(__dirname, '.venv', 'bin', 'ruff');
// The project's pinned ruff; the one on PATH can be any version.
const ruff = fs.existsSync(venvRuff) ? `'${venvRuff}'` : 'ruff';
const quote = (files) => files.map((f) => `'${f}'`).join(' ');

module.exports = {
    '*.py': [`${ruff} check --fix`, `${ruff} format`],
    'frontend/**/*.{ts,tsx,js,jsx,json,css,html,md}': (files) => {
        const ignore = path.join(frontendDir, '.prettierignore');
        const cmds = [`${bin('prettier')} --ignore-path '${ignore}' --write ${quote(files)}`];
        const lintable = files.filter((f) => /\.(ts|tsx|js|jsx)$/.test(f));
        if (lintable.length) {
            cmds.push(`${bin('eslint')} --fix --config ${path.join(frontendDir, 'eslint.config.cjs')} ${quote(lintable)}`);
        }
        return cmds;
    }
};
