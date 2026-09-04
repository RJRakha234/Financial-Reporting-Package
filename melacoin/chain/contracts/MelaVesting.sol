// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/**
 * @title MelaVesting
 * @notice Cliff + linear vesting for team and advisor allocations.
 *
 * Why this exists: an exchange, and anyone reading your tokenomics, will ask
 * "what stops the founders selling on day one?". A published, on-chain vesting
 * contract is the only answer that does not require trusting you. Lock the team
 * allocation here at launch and link the address in your docs.
 *
 * Each beneficiary gets one schedule: nothing before `cliff`, then a straight line
 * to 100% at `start + duration`. Schedules are revocable only if created that way,
 * and revoking returns just the *unvested* remainder - already-vested tokens stay
 * claimable by the beneficiary.
 */
contract MelaVesting is Ownable {
    using SafeERC20 for IERC20;

    struct Schedule {
        uint128 total;      // total tokens allocated
        uint128 released;   // tokens already withdrawn
        uint64 start;       // unix time vesting begins
        uint64 cliff;       // unix time before which nothing is claimable
        uint64 duration;    // seconds from start to fully vested
        uint64 revokedAt;   // 0 = live; otherwise vesting is frozen at this timestamp
        bool revocable;
    }

    IERC20 public immutable token;
    mapping(address => Schedule) public schedules;

    /// @notice Tokens committed to schedules; the contract may not hold less than this.
    uint256 public totalCommitted;

    event ScheduleCreated(address indexed beneficiary, uint256 total, uint64 start, uint64 cliff, uint64 duration);
    event Released(address indexed beneficiary, uint256 amount);
    event Revoked(address indexed beneficiary, uint256 refunded);

    error ZeroAddress();
    error ZeroAmount();
    error ScheduleExists(address beneficiary);
    error NoSchedule(address beneficiary);
    error CliffBeforeStart();
    error CliffAfterEnd();
    error NothingToRelease();
    error NotRevocable();
    error AlreadyRevoked();
    error UnderFunded(uint256 needed, uint256 have);

    constructor(address token_, address owner_) Ownable(owner_) {
        if (token_ == address(0)) revert ZeroAddress();
        token = IERC20(token_);
    }

    /// @notice Create a schedule. The contract must already hold enough unspoken-for tokens.
    function createSchedule(
        address beneficiary,
        uint256 total,
        uint64 start,
        uint64 cliff,
        uint64 duration,
        bool revocable
    ) external onlyOwner {
        if (beneficiary == address(0)) revert ZeroAddress();
        if (total == 0 || duration == 0) revert ZeroAmount();
        if (schedules[beneficiary].total != 0) revert ScheduleExists(beneficiary);
        if (cliff < start) revert CliffBeforeStart();
        if (cliff > start + duration) revert CliffAfterEnd();

        uint256 needed = totalCommitted + total;
        uint256 have = token.balanceOf(address(this));
        if (have < needed) revert UnderFunded(needed, have);

        schedules[beneficiary] = Schedule({
            total: uint128(total),
            released: 0,
            start: start,
            cliff: cliff,
            duration: duration,
            revokedAt: 0,
            revocable: revocable
        });
        totalCommitted = needed;

        emit ScheduleCreated(beneficiary, total, start, cliff, duration);
    }

    /// @notice Withdraw everything vested and not yet released, for the caller.
    function release() external {
        _release(msg.sender);
    }

    /// @notice Withdraw on someone else's behalf; tokens still go to the beneficiary.
    function releaseFor(address beneficiary) external {
        _release(beneficiary);
    }

    function _release(address beneficiary) private {
        Schedule storage s = schedules[beneficiary];
        if (s.total == 0) revert NoSchedule(beneficiary);

        uint256 amount = releasable(beneficiary);
        if (amount == 0) revert NothingToRelease();

        s.released += uint128(amount);
        totalCommitted -= amount;

        emit Released(beneficiary, amount);
        token.safeTransfer(beneficiary, amount);
    }

    /// @notice Cancel the *unvested* part of a revocable schedule and return it to `owner`.
    /// @dev Already-vested tokens stay claimable by the beneficiary forever.
    function revoke(address beneficiary) external onlyOwner {
        Schedule storage s = schedules[beneficiary];
        if (s.total == 0) revert NoSchedule(beneficiary);
        if (!s.revocable) revert NotRevocable();
        if (s.revokedAt != 0) revert AlreadyRevoked();

        uint256 vested = vestedAmount(beneficiary, uint64(block.timestamp));
        uint256 refund = s.total - vested;

        // Freeze the curve here. `total` is deliberately left alone: the beneficiary
        // keeps every token vested up to this moment and simply stops accruing more.
        s.revokedAt = uint64(block.timestamp);
        totalCommitted -= refund;

        emit Revoked(beneficiary, refund);
        if (refund > 0) token.safeTransfer(owner(), refund);
    }

    /// @notice Total vested for `beneficiary` at time `at` (released or not).
    /// @dev After a revocation the clock stops, so this never grows past the revocation time.
    function vestedAmount(address beneficiary, uint64 at) public view returns (uint256) {
        Schedule memory s = schedules[beneficiary];
        if (s.total == 0) return 0;
        if (s.revokedAt != 0 && at > s.revokedAt) at = s.revokedAt;
        if (at < s.cliff) return 0;
        if (at >= s.start + s.duration) return s.total;
        return (uint256(s.total) * (at - s.start)) / s.duration;
    }

    /// @notice Vested but not yet withdrawn.
    function releasable(address beneficiary) public view returns (uint256) {
        Schedule memory s = schedules[beneficiary];
        return vestedAmount(beneficiary, uint64(block.timestamp)) - s.released;
    }

    /// @notice Tokens sitting here that are not promised to any schedule.
    function unallocatedBalance() external view returns (uint256) {
        return token.balanceOf(address(this)) - totalCommitted;
    }
}
