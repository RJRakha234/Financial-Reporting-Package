"use strict";
/**
 * counter.service.js - the two things a shop does at its own till that nothing
 * else in the system could do for it: sign a customer up, and undo a mistake.
 *
 * Both exist because of what a real counter is like. A chai stall has no billing
 * machine, ten seconds per sale, and a queue at 8am. A customer will give a phone
 * number and nothing else - no email, no password, certainly no app download. And
 * within the first hour somebody will type 200 instead of 20.
 */
const db = require("../db");
const auth = require("../auth");
const money = require("../money");
const config = require("../config");
const { RuleError } = require("../errors");
const tokenService = require("./token.service");
const loyalty = require("./loyalty.service");

/**
 * An account created at a counter has no password yet. This value can never match
 * a real scrypt hash, so the account cannot be logged into until the customer
 * claims it and chooses a password.
 */
const UNCLAIMED = "unclaimed";

/**
 * Placeholder address for a counter-enrolled customer. `.invalid` is reserved by
 * RFC 2606 and can never be a real domain, so this can never collide with, or be
 * mistaken for, somebody's actual email. It is hidden from every screen.
 */
const placeholderEmail = (phone) => `p.${phone}@melacoin.invalid`;
const isPlaceholder = (email) => typeof email === "string" && email.endsWith("@melacoin.invalid");

/**
 * What we call someone before anybody tells us their name. Recognisable at a
 * counter (it ends in the last four digits they just read out) and detectable
 * later, so a shop that learns the real name can replace it.
 */
const placeholderName = (phone) => `Customer ${phone.slice(-4)}`;

/**
 * Turns whatever the shopkeeper typed into a canonical 10-digit Indian mobile
 * number. "+91 98000 00001", "098000-00001" and "9800000001" are one person.
 */
function normalisePhone(input) {
  const digits = String(input || "").replace(/\D/g, "");
  const local = digits.length === 12 && digits.startsWith("91") ? digits.slice(2)
    : digits.length === 11 && digits.startsWith("0") ? digits.slice(1)
    : digits;
  if (!/^[6-9]\d{9}$/.test(local)) {
    throw new RuleError("Enter a 10-digit mobile number starting with 6, 7, 8 or 9");
  }
  return local;
}

function findByPhone(phone) {
  return db.get().prepare("SELECT * FROM users WHERE phone = ? AND role = 'customer'").get(phone) || null;
}

/**
 * Signs a customer up from the counter with nothing but a phone number.
 *
 * Idempotent on purpose: a shopkeeper who taps twice, or who enrols a customer
 * another shop already enrolled, gets the same person back rather than an error.
 * The customer belongs to the network, not to the shop that happened to add them.
 */
function enrol({ phone, name = null }) {
  const number = normalisePhone(phone);
  const existing = findByPhone(number);
  if (existing) {
    // A real name beats the placeholder we invented. A real name already on file
    // is left alone - the customer's own choice outranks a shop's guess.
    const knownName = (existing.name || "").trim();
    const stillPlaceholder = knownName === "" || knownName === placeholderName(number);
    if (name && name.trim() && stillPlaceholder) {
      db.get().prepare("UPDATE users SET name = ? WHERE id = ?").run(name.trim(), existing.id);
    }
    return { customer: publicCustomer(findByPhone(number)), created: false };
  }

  const id = db.newId("cus");
  db.get()
    .prepare("INSERT INTO users (id, email, phone, name, role, password_hash, created_at) VALUES (?,?,?,?,?,?,?)")
    .run(id, placeholderEmail(number), number, (name || "").trim() || placeholderName(number),
         "customer", UNCLAIMED, db.now());

  return { customer: publicCustomer(db.get().prepare("SELECT * FROM users WHERE id = ?").get(id)), created: true };
}

/** Never leak the placeholder address or the password column to a screen. */
function publicCustomer(user) {
  return {
    id: user.id,
    name: user.name,
    phone: user.phone,
    email: isPlaceholder(user.email) ? null : user.email,
    claimed: user.password_hash !== UNCLAIMED,
    created_at: user.created_at,
  };
}

/**
 * Lets a customer take ownership of an account a shop created for them, by
 * registering with the same phone number. Without this, anyone enrolled at a
 * counter could never reach their own balance - a dead end we would only discover
 * once somebody asked.
 */
function claim({ phone, name, email, password }) {
  const number = normalisePhone(phone);
  const existing = findByPhone(number);
  if (!existing) return null;
  if (existing.password_hash !== UNCLAIMED) {
    throw new RuleError("An account with that phone number already exists. Sign in instead.");
  }

  const taken = db.get().prepare("SELECT 1 FROM users WHERE email = ? AND id != ?").get(email, existing.id);
  if (taken) throw new RuleError("An account with that email already exists");

  db.get()
    .prepare("UPDATE users SET name = ?, email = ?, password_hash = ? WHERE id = ?")
    .run(name, email, auth.hashPassword(password), existing.id);

  return db.get().prepare("SELECT * FROM users WHERE id = ?").get(existing.id);
}

// --------------------------------------------------------------------- voiding

/**
 * Undoes a bill: gives back the points it took, takes back the points it gave,
 * returns any MelaCoin, and reverses the shop's settlement entry.
 *
 * Deliberately narrow. It refuses rather than guesses when unwinding would be
 * ambiguous, because a wrong reversal is worse than no reversal: it would silently
 * take points off a customer who had already earned and spent them.
 */
function voidPurchase({ vendor, purchaseId, reason = null }) {
  const purchase = db.get().prepare("SELECT * FROM purchases WHERE id = ?").get(purchaseId);
  if (!purchase) throw new RuleError("That bill was not found");
  if (purchase.vendor_id !== vendor.id) throw new RuleError("That bill belongs to another shop");
  if (purchase.voided_at) throw new RuleError("That bill has already been cancelled");

  const ageMinutes = (Date.now() - Date.parse(purchase.created_at)) / 60000;
  if (ageMinutes > config.voidWindowMinutes) {
    throw new RuleError(
      `Bills can only be cancelled within ${config.voidWindowMinutes} minutes. ` +
        `This one is ${Math.round(ageMinutes)} minutes old — ring up a correction instead.`
    );
  }

  // The points this bill GAVE must be untouched, or removing them would take
  // value the customer has already legitimately spent somewhere.
  const earnedLot = db.get().prepare("SELECT * FROM point_lots WHERE purchase_id = ?").get(purchaseId);
  if (earnedLot && earnedLot.points_remaining !== earnedLot.points_earned) {
    throw new RuleError(
      "This bill cannot be cancelled because the customer has already spent the points it gave them."
    );
  }

  const at = db.now();
  db.transaction((database) => {
    const logEvent = database.prepare(
      `INSERT INTO point_events (id, vendor_id, customer_id, kind, points_delta, lot_id, ref_type, ref_id, note, created_at)
       VALUES (?, ?, ?, 'ADJUST', ?, ?, 'void', ?, ?, ?)`
    );

    // 1. Take back the points the bill gave.
    if (earnedLot && earnedLot.points_remaining > 0) {
      database.prepare("UPDATE point_lots SET points_remaining = 0 WHERE id = ?").run(earnedLot.id);
      logEvent.run(db.newId("evt"), vendor.id, purchase.customer_id, -earnedLot.points_remaining,
                   earnedLot.id, purchaseId, "cancelled bill: points withdrawn", at);
    }

    // 2. Put back the points the bill spent, into the exact lots they came from.
    const spent = database
      .prepare("SELECT * FROM point_events WHERE ref_id = ? AND kind = 'REDEEM'")
      .all(purchaseId);
    const restore = database.prepare("UPDATE point_lots SET points_remaining = points_remaining + ? WHERE id = ?");
    for (const event of spent) {
      const amount = -event.points_delta; // stored negative
      restore.run(amount, event.lot_id);
      logEvent.run(db.newId("evt"), vendor.id, purchase.customer_id, amount, event.lot_id,
                   purchaseId, "cancelled bill: points returned", at);
    }

    // 3. Give back any MelaCoin, and undo what the platform owed the shop for it.
    if (purchase.mela_wei_paid !== "0") {
      tokenService.post({
        customerId: purchase.customer_id, kind: "ADJUST", weiDelta: BigInt(purchase.mela_wei_paid),
        refType: "void", refId: purchaseId, note: "cancelled bill: MelaCoin returned",
      });
      loyalty.recordSettlement(database, {
        vendorId: vendor.id, kind: "ADJUST", amountPaise: purchase.mela_discount_paise,
        refType: "void", refId: purchaseId, note: "cancelled bill: MelaCoin acceptance reversed", at,
      });
    }

    database
      .prepare("UPDATE purchases SET voided_at = ?, void_reason = ? WHERE id = ?")
      .run(at, reason, purchaseId);
  });

  return {
    purchase: db.get().prepare("SELECT * FROM purchases WHERE id = ?").get(purchaseId),
    points_balance: loyalty.pointsBalance(purchase.customer_id, vendor.id),
  };
}

/** The bills a shop could still cancel right now. What the "undo" screen lists. */
function voidableBills(vendorId, limit = 10) {
  const cutoff = new Date(Date.now() - config.voidWindowMinutes * 60000).toISOString();
  return db
    .get()
    .prepare(
      `SELECT p.*, u.name AS customer_name, u.phone AS customer_phone
       FROM purchases p JOIN users u ON u.id = p.customer_id
       WHERE p.vendor_id = ? AND p.voided_at IS NULL AND p.created_at >= ?
       ORDER BY p.created_at DESC LIMIT ?`
    )
    .all(vendorId, cutoff, limit)
    .map((row) => ({
      ...row,
      display: { gross: money.formatPaise(row.gross_paise), net: money.formatPaise(row.net_paise) },
    }));
}

module.exports = {
  enrol, claim, normalisePhone, findByPhone, publicCustomer, isPlaceholder,
  voidPurchase, voidableBills, UNCLAIMED,
};
