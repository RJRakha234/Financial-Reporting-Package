"use strict";
/**
 * validate.js - turn whatever arrived over the network into the exact types we want,
 * or fail with a message a human can act on. Never trust a request body.
 */
const { badRequest } = require("./http");

function requiredString(body, field, { min = 1, max = 200, pattern = null } = {}) {
  const value = body[field];
  if (typeof value !== "string" || value.trim().length < min) {
    throw badRequest(`"${field}" is required`);
  }
  const trimmed = value.trim();
  if (trimmed.length > max) throw badRequest(`"${field}" must be at most ${max} characters`);
  if (pattern && !pattern.test(trimmed)) throw badRequest(`"${field}" is not in the expected format`);
  return trimmed;
}

function optionalString(body, field, options = {}) {
  if (body[field] === undefined || body[field] === null || body[field] === "") return null;
  return requiredString(body, field, options);
}

function requiredInt(body, field, { min = 0, max = Number.MAX_SAFE_INTEGER } = {}) {
  const value = body[field];
  const parsed = typeof value === "string" && value.trim() !== "" ? Number(value) : value;
  if (!Number.isInteger(parsed)) throw badRequest(`"${field}" must be a whole number`);
  if (parsed < min || parsed > max) throw badRequest(`"${field}" must be between ${min} and ${max}`);
  return parsed;
}

function optionalInt(body, field, options = {}) {
  if (body[field] === undefined || body[field] === null || body[field] === "") return null;
  return requiredInt(body, field, options);
}

function optionalBool(body, field) {
  const value = body[field];
  if (value === undefined || value === null) return null;
  if (typeof value === "boolean") return value;
  if (value === "true" || value === 1 || value === "1") return true;
  if (value === "false" || value === 0 || value === "0") return false;
  throw badRequest(`"${field}" must be true or false`);
}

function optionalBigInt(body, field) {
  const value = body[field];
  if (value === undefined || value === null || value === "") return 0n;
  try {
    const parsed = BigInt(value);
    if (parsed < 0n) throw new Error("negative");
    return parsed;
  } catch {
    throw badRequest(`"${field}" must be a whole token amount in wei`);
  }
}

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const email = (body, field = "email") => requiredString(body, field, { pattern: EMAIL, max: 254 }).toLowerCase();

/** "Chai Corner, Koramangala" -> "chai-corner-koramangala" */
function slugify(name) {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
}

module.exports = {
  requiredString, optionalString, requiredInt, optionalInt,
  optionalBool, optionalBigInt, email, slugify, EMAIL,
};
