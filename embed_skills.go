package main

import "embed"

// SkillsFS embeds all agent skills from .agents/skills/.
// The all: prefix is required to include directories starting with a dot.
// These skills follow the agentskills.io open standard and can be installed
// to ~/.agents/skills/ (user-level) or ./.agents/skills/ (project-level).
//
//go:embed all:.agents/skills
var SkillsFS embed.FS
