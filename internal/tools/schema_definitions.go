// Package tools provides MCP tool implementations for IBM Cloud Logs.
// This file provides shared JSON Schema definitions to reduce redundancy
// and optimize descriptions for 2025 "Attention Steering" patterns.
package tools

import "fmt"

// SchemaDefinitions provides reusable JSON Schema components.
// Using $ref patterns reduces token overhead in tool descriptions
// and ensures consistency across all tool schemas.
var SchemaDefinitions = map[string]interface{}{
	"$schema": "https://json-schema.org/draft/2020-12/schema",
	"definitions": map[string]interface{}{
		// Common ID reference
		"resourceId": map[string]interface{}{
			"type":        "string",
			"description": "Unique resource identifier (UUID)",
			"pattern":     "^[a-f0-9-]{36}$",
		},

		// Pagination schema
		"pagination": map[string]interface{}{
			"type": "object",
			"properties": map[string]interface{}{
				"limit": map[string]interface{}{
					"type":        "integer",
					"description": "Max results (default: 50, max: 100)",
					"default":     50,
					"minimum":     1,
					"maximum":     100,
				},
				"cursor": map[string]interface{}{
					"type":        "string",
					"description": "Pagination cursor from previous response",
				},
			},
		},

		// Date range schema
		"dateRange": map[string]interface{}{
			"type": "object",
			"properties": map[string]interface{}{
				"start_date": map[string]interface{}{
					"type":        "string",
					"format":      "date-time",
					"description": "Start time (ISO 8601)",
				},
				"end_date": map[string]interface{}{
					"type":        "string",
					"format":      "date-time",
					"description": "End time (ISO 8601)",
				},
			},
			"required": []string{"start_date", "end_date"},
		},

		// Query tier enum
		"queryTier": map[string]interface{}{
			"type":        "string",
			"enum":        []string{"archive", "frequent_search"},
			"default":     "archive",
			"description": "Log tier: archive (historical) or frequent_search (real-time)",
		},

		// Query syntax enum
		"querySyntax": map[string]interface{}{
			"type":        "string",
			"enum":        []string{"dataprime", "lucene"},
			"default":     "dataprime",
			"description": "Query language: dataprime (recommended) or lucene",
		},

		// Severity levels
		"severity": map[string]interface{}{
			"type":        "string",
			"enum":        []string{"debug", "verbose", "info", "warning", "error", "critical"},
			"description": "Log severity level",
		},

		// Common name field
		"resourceName": map[string]interface{}{
			"type":        "string",
			"description": "Human-readable name",
			"minLength":   1,
			"maxLength":   4096,
		},

		// Description field
		"resourceDescription": map[string]interface{}{
			"type":        "string",
			"description": "Optional description",
			"maxLength":   4096,
		},

		// Boolean confirmation for destructive operations
		"confirmDelete": map[string]interface{}{
			"type":        "boolean",
			"default":     false,
			"description": "Set true to confirm deletion (prevents accidents)",
		},

		// Dry-run flag
		"dryRun": map[string]interface{}{
			"type":        "boolean",
			"default":     false,
			"description": "Validate without executing (preview mode)",
		},

		// Application filter
		"applicationFilter": map[string]interface{}{
			"type":        "string",
			"description": "Filter by application name (accepts aliases: namespace, app, service)",
		},

		// Subsystem filter
		"subsystemFilter": map[string]interface{}{
			"type":        "string",
			"description": "Filter by subsystem (accepts aliases: component, module, resource)",
		},

		// DataPrime query
		"dataPrimeQuery": map[string]interface{}{
			"type":        "string",
			"description": "DataPrime query. Syntax: source logs | filter <condition> | limit N",
			"minLength":   1,
			"maxLength":   4096,
			"examples": []string{
				"source logs | filter $m.severity >= 5 | limit 100",
				"source logs | filter $l.applicationname == 'myapp' | limit 50",
			},
		},

		// Alert configuration
		"alertConfig": map[string]interface{}{
			"type": "object",
			"properties": map[string]interface{}{
				"name":                  map[string]interface{}{"$ref": "#/definitions/resourceName"},
				"is_active":             map[string]interface{}{"type": "boolean", "default": true},
				"alert_definition_id":   map[string]interface{}{"$ref": "#/definitions/resourceId"},
				"notification_group_id": map[string]interface{}{"$ref": "#/definitions/resourceId"},
			},
			"required": []string{"name"},
		},

		// Dashboard widget
		"dashboardWidget": map[string]interface{}{
			"type": "object",
			"properties": map[string]interface{}{
				"id":   map[string]interface{}{"$ref": "#/definitions/resourceId"},
				"type": map[string]interface{}{"type": "string"},
				"definition": map[string]interface{}{
					"type":        "object",
					"description": "Widget-specific configuration",
				},
			},
		},

		// Policy priority
		"policyPriority": map[string]interface{}{
			"type":        "string",
			"enum":        []string{"type_unspecified", "block", "low", "medium", "high"},
			"description": "Policy priority level",
		},
	},
}

// RefSchema creates a JSON Schema $ref reference
func RefSchema(definitionName string) map[string]interface{} {
	return map[string]interface{}{
		"$ref": "#/definitions/" + definitionName,
	}
}

// MergeSchemaProperties merges schema definitions with custom properties
func MergeSchemaProperties(base map[string]interface{}, custom map[string]interface{}) map[string]interface{} {
	result := make(map[string]interface{})

	// Copy base properties
	for k, v := range base {
		result[k] = v
	}

	// Override with custom properties
	for k, v := range custom {
		result[k] = v
	}

	return result
}

// ========================================================================
// Optimized Schema Builders for Attention Steering
// ========================================================================

// QueryInputSchema returns the optimized schema for query tools.
// This schema is designed to minimize tokens while maximizing clarity
// for LLM attention steering.
func QueryInputSchema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"query":           RefSchema("dataPrimeQuery"),
			"tier":            RefSchema("queryTier"),
			"syntax":          RefSchema("querySyntax"),
			"start_date":      map[string]interface{}{"type": "string", "format": "date-time"},
			"end_date":        map[string]interface{}{"type": "string", "format": "date-time"},
			"limit":           map[string]interface{}{"type": "integer", "default": 200, "maximum": 50000},
			"summary_only":    map[string]interface{}{"type": "boolean", "default": false, "description": "Return stats only (~90% fewer tokens)"},
			"applicationName": RefSchema("applicationFilter"),
			"subsystemName":   RefSchema("subsystemFilter"),
		},
		"required": []string{"query", "start_date", "end_date"},
	}
}

// CRUDGetSchema returns the schema for GET operations
func CRUDGetSchema(resourceName string) map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"id": map[string]interface{}{
				"type":        "string",
				"description": "The " + resourceName + " ID",
			},
		},
		"required": []string{"id"},
	}
}

// CRUDListSchema returns the schema for LIST operations
func CRUDListSchema() map[string]interface{} {
	return map[string]interface{}{
		"type":       "object",
		"properties": StandardPaginationSchema(),
	}
}

// CRUDCreateSchema returns the schema for CREATE operations
func CRUDCreateSchema(resourceName string, example map[string]interface{}) map[string]interface{} {
	props := map[string]interface{}{
		resourceName: map[string]interface{}{
			"type":        "object",
			"description": "The " + resourceName + " configuration",
		},
		"dry_run": RefSchema("dryRun"),
	}

	if example != nil {
		props[resourceName].(map[string]interface{})["example"] = example
	}

	return map[string]interface{}{
		"type":       "object",
		"properties": props,
		"required":   []string{resourceName},
	}
}

// CRUDUpdateSchema returns the schema for UPDATE operations
func CRUDUpdateSchema(resourceName string) map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"id": map[string]interface{}{
				"type":        "string",
				"description": "The " + resourceName + " ID to update",
			},
			resourceName: map[string]interface{}{
				"type":        "object",
				"description": "Updated configuration",
			},
		},
		"required": []string{"id", resourceName},
	}
}

// CRUDDeleteSchema returns the schema for DELETE operations
func CRUDDeleteSchema(resourceName string) map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"id": map[string]interface{}{
				"type":        "string",
				"description": "The " + resourceName + " ID to delete",
			},
			"confirm": RefSchema("confirmDelete"),
		},
		"required": []string{"id"},
	}
}

// ========================================================================
// Compact Description Generators
// ========================================================================

// CompactQueryDescription returns an optimized description for query tools
// that maximizes information density for LLM attention.
func CompactQueryDescription(currentDate string) string {
	return `Query IBM Cloud Logs (default method).

**Date:** ` + currentDate + `
**Syntax:** source logs | filter <cond> | limit N

**Quick refs:**
- $l.applicationname, $l.subsystemname (labels)
- $m.severity: DEBUG(1)-CRITICAL(6)
- $d.field (log payload)

**Related:** build_query (helper), get_dataprime_reference (docs), submit_background_query (large queries)`
}

// CompactCRUDDescription returns optimized descriptions for CRUD tools
func CompactCRUDDescription(operation, resourceName, relatedTools string) string {
	switch operation {
	case "get":
		return "Get " + resourceName + " by ID. Related: " + relatedTools
	case "list":
		return "List all " + resourceName + "s. Use before create (check duplicates) or to find IDs."
	case "create":
		return "Create " + resourceName + ". Use dry_run=true to validate first."
	case "update":
		return "Update " + resourceName + ". Get current config first with get_" + resourceName + "."
	case "delete":
		return "Delete " + resourceName + ". Requires confirm=true to prevent accidents."
	default:
		return operation + " " + resourceName
	}
}

// ========================================================================
// Output Schema Builders
// ========================================================================

// QueryOutputSchema returns the output schema for query results
func QueryOutputSchema() map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			"events": map[string]interface{}{
				"type":  "array",
				"items": map[string]interface{}{"type": "object"},
			},
			"_summary": map[string]interface{}{
				"type": "object",
				"properties": map[string]interface{}{
					"total_events":  map[string]string{"type": "integer"},
					"severity_dist": map[string]string{"type": "object"},
					"time_range":    map[string]string{"type": "string"},
				},
			},
			"_pagination": map[string]interface{}{
				"type": "object",
				"properties": map[string]interface{}{
					"has_more":       map[string]string{"type": "boolean"},
					"last_timestamp": map[string]string{"type": "string"},
				},
			},
		},
	}
}

// ResourceOutputSchema returns a generic output schema for resource operations
func ResourceOutputSchema(resourceName string, additionalFields map[string]interface{}) map[string]interface{} {
	props := map[string]interface{}{
		"id":   map[string]string{"type": "string"},
		"name": map[string]string{"type": "string"},
	}

	for k, v := range additionalFields {
		props[k] = v
	}

	return map[string]interface{}{
		"type":        "object",
		"title":       resourceName,
		"description": fmt.Sprintf("Output schema for %s resource", resourceName),
		"properties":  props,
	}
}
