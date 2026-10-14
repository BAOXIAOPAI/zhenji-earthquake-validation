// Controlled driver for terminal-state verification without model calls.
//
// The guide's four terminal regressions must run through the production
// `_execute` — the real subprocess, the real MCP tool layer, the real validator
// and the real outcome record — without a paid model. This wrapper keeps the
// `tsx main/<script> <run_id> <model>` contract `_execute` expects and hands the
// actual scripted tool scenario to `main/controlled_driver.py`, which imports the
// production MCP server in-process and calls the same tools a session would.
//
// The scenario is selected with CONTROLLED_SCENARIO; the driver never contacts a
// model provider and never starts the AGH session.
import {spawnSync} from 'node:child_process';
import {join, resolve} from 'node:path';

const project=resolve('.');
const RUN=process.argv[2];
const scenario=process.env.CONTROLLED_SCENARIO||'acquire_error_unrecovered';
const py=process.env.EQ_PY||join(project,'.venv/bin/python');
const child=spawnSync(py,[join(project,'main/controlled_driver.py'),RUN,scenario],
  {stdio:'inherit'});
process.exit(child.status??2);
