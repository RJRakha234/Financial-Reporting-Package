"use strict";
/**
 * A RuleError means "the request was understood, but the rules of the business say
 * no" - not enough points, shop is closed, bill too small. These become a 400 with
 * the message shown to the user.
 *
 * Anything else that reaches the server is a bug, becomes a 500, and shows the user
 * a generic message while the real stack trace goes to the log. Keeping these two
 * apart is what stops "not enough points" and "the database is on fire" from
 * looking the same to whoever is on call.
 */
class RuleError extends Error {
  constructor(message) {
    super(message);
    this.name = "RuleError";
    this.status = 400;
  }
}

module.exports = { RuleError };
