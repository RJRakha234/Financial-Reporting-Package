// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ERC20Burnable} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Burnable.sol";
import {IERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Permit.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/**
 * @title MelaDistributor
 * @notice The bridge between the off-chain loyalty app and the on-chain token.
 *
 * Two jobs:
 *
 * 1. WITHDRAW (off-chain -> on-chain).
 *    When a customer converts loyalty points into MELA, the backend does NOT send a
 *    transaction. It signs a "voucher" (a plain message saying: this address may take
 *    this many MELA, once, before this deadline). The customer - or a relayer paying
 *    gas on their behalf - submits it with `claim`. Each voucher has a unique nonce
 *    that is burned on use, so a replayed voucher reverts.
 *
 *    Why a pull model? A hot backend wallet that sends thousands of transfers is the
 *    #1 thing that gets drained. Here the reserve sits in this contract, the backend
 *    key can only *authorise* amounts, and the daily cap below bounds the damage if
 *    that key ever leaks.
 *
 * 2. SPEND (on-chain -> off-chain).
 *    `spend` burns the customer's MELA and emits an event naming the vendor. The
 *    backend watches for that event and credits the vendor in rupees from treasury.
 *    Burning - not transferring to us - is the point: spent MELA leaves supply
 *    forever, so real usage shrinks the float instead of recycling it.
 */
contract MelaDistributor is EIP712, AccessControl, Pausable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    bytes32 public constant SIGNER_ROLE = keccak256("SIGNER_ROLE");
    bytes32 public constant TREASURER_ROLE = keccak256("TREASURER_ROLE");
    bytes32 public constant PAUSER_ROLE = keccak256("PAUSER_ROLE");

    bytes32 public constant CLAIM_TYPEHASH =
        keccak256("Claim(address to,uint256 amount,uint256 nonce,uint256 deadline)");

    IERC20 public immutable melaCoin;

    /// @notice Vouchers already redeemed. nonce => used.
    mapping(uint256 => bool) public nonceUsed;

    /// @notice Rolling 24h payout ceiling. 0 means "no limit" (not recommended).
    uint256 public dailyClaimCap;
    uint256 public claimedInWindow;
    uint256 public windowStart;

    event Claimed(address indexed to, uint256 amount, uint256 indexed nonce);
    event Spent(address indexed from, uint256 amount, bytes32 indexed vendorRef);
    event Funded(address indexed from, uint256 amount);
    event DailyClaimCapUpdated(uint256 newCap);
    event Swept(address indexed to, uint256 amount);

    error ZeroAddress();
    error ZeroAmount();
    error VoucherExpired(uint256 deadline);
    error VoucherAlreadyUsed(uint256 nonce);
    error BadSignature();
    error DailyCapExceeded(uint256 requested, uint256 remaining);
    error InsufficientReserve(uint256 requested, uint256 available);

    constructor(address melaCoin_, address admin, uint256 dailyClaimCap_)
        EIP712("MelaDistributor", "1")
    {
        if (melaCoin_ == address(0) || admin == address(0)) revert ZeroAddress();
        melaCoin = IERC20(melaCoin_);
        dailyClaimCap = dailyClaimCap_;
        windowStart = block.timestamp;

        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(SIGNER_ROLE, admin);
        _grantRole(TREASURER_ROLE, admin);
        _grantRole(PAUSER_ROLE, admin);
    }

    // ---------------------------------------------------------------- withdraw

    /**
     * @notice Redeem a backend-signed voucher for MELA.
     * @dev Callable by anyone (so a relayer can pay gas); tokens always go to `to`.
     */
    function claim(address to, uint256 amount, uint256 nonce, uint256 deadline, bytes calldata signature)
        external
        nonReentrant
        whenNotPaused
    {
        if (to == address(0)) revert ZeroAddress();
        if (amount == 0) revert ZeroAmount();
        if (block.timestamp > deadline) revert VoucherExpired(deadline);
        if (nonceUsed[nonce]) revert VoucherAlreadyUsed(nonce);

        bytes32 digest = _hashTypedDataV4(keccak256(abi.encode(CLAIM_TYPEHASH, to, amount, nonce, deadline)));
        address signer = ECDSA.recover(digest, signature);
        if (!hasRole(SIGNER_ROLE, signer)) revert BadSignature();

        uint256 available = melaCoin.balanceOf(address(this));
        if (amount > available) revert InsufficientReserve(amount, available);

        nonceUsed[nonce] = true;
        _consumeDailyAllowance(amount);

        emit Claimed(to, amount, nonce);
        melaCoin.safeTransfer(to, amount);
    }

    function _consumeDailyAllowance(uint256 amount) private {
        if (dailyClaimCap == 0) return;

        if (block.timestamp >= windowStart + 1 days) {
            windowStart = block.timestamp;
            claimedInWindow = 0;
        }
        uint256 remaining = dailyClaimCap - claimedInWindow;
        if (amount > remaining) revert DailyCapExceeded(amount, remaining);
        claimedInWindow += amount;
    }

    // ------------------------------------------------------------------- spend

    /**
     * @notice Burn `amount` MELA to pay a vendor. Requires an ERC-20 allowance first.
     * @param vendorRef Opaque vendor identifier the backend can resolve (e.g. keccak of vendor slug).
     */
    function spend(uint256 amount, bytes32 vendorRef) public whenNotPaused {
        if (amount == 0) revert ZeroAmount();
        emit Spent(msg.sender, amount, vendorRef);
        ERC20Burnable(address(melaCoin)).burnFrom(msg.sender, amount);
    }

    /// @notice One-transaction spend: approve via EIP-2612 signature, then burn.
    function spendWithPermit(
        uint256 amount,
        bytes32 vendorRef,
        uint256 deadline,
        uint8 v,
        bytes32 r,
        bytes32 s
    ) external {
        // A griefer can front-run the permit; ignore the revert and let spend() decide,
        // since spend() fails anyway if the allowance is genuinely missing.
        try IERC20Permit(address(melaCoin)).permit(msg.sender, address(this), amount, deadline, v, r, s) {} catch {}
        spend(amount, vendorRef);
    }

    // -------------------------------------------------------------- treasury

    /// @notice Move MELA into the reserve. Caller must approve first.
    function fund(uint256 amount) external {
        if (amount == 0) revert ZeroAmount();
        emit Funded(msg.sender, amount);
        melaCoin.safeTransferFrom(msg.sender, address(this), amount);
    }

    function setDailyClaimCap(uint256 newCap) external onlyRole(DEFAULT_ADMIN_ROLE) {
        dailyClaimCap = newCap;
        emit DailyClaimCapUpdated(newCap);
    }

    /// @notice Pull unused reserve back to treasury.
    function sweep(address to, uint256 amount) external onlyRole(TREASURER_ROLE) {
        if (to == address(0)) revert ZeroAddress();
        emit Swept(to, amount);
        melaCoin.safeTransfer(to, amount);
    }

    function pause() external onlyRole(PAUSER_ROLE) {
        _pause();
    }

    function unpause() external onlyRole(PAUSER_ROLE) {
        _unpause();
    }

    // ----------------------------------------------------------------- views

    /// @notice MELA currently available to satisfy vouchers.
    function reserveBalance() external view returns (uint256) {
        return melaCoin.balanceOf(address(this));
    }

    /// @notice How much may still be claimed in the current 24h window.
    function remainingDailyAllowance() external view returns (uint256) {
        if (dailyClaimCap == 0) return type(uint256).max;
        if (block.timestamp >= windowStart + 1 days) return dailyClaimCap;
        return dailyClaimCap - claimedInWindow;
    }
}
