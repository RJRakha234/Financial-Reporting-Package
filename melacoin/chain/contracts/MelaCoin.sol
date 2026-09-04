// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC20Burnable} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Burnable.sol";
import {ERC20Capped} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Capped.sol";
import {ERC20Pausable} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Pausable.sol";
import {ERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Permit.sol";
import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

/**
 * @title MelaCoin
 * @notice The MelaCoin network token (MELA).
 *
 * Design notes, in plain English:
 *  - Hard cap: no more than `cap()` MELA can ever exist. Minting past the cap reverts.
 *    This is what makes the supply promise credible to an exchange or a buyer.
 *  - Minting is *not* open. Only addresses holding MINTER_ROLE can mint, and the
 *    intended holder is a multisig that releases tokens on the published schedule.
 *  - Burnable: a customer who spends MELA at a vendor burns it, which permanently
 *    removes it from supply. Burning is how the token gets used up, not "resold".
 *  - Pausable: an emergency stop for transfers. It is a blunt tool, so PAUSER_ROLE
 *    should also live on a multisig, never on a hot backend key.
 *  - Permit (EIP-2612): lets a user approve spending with a signature instead of a
 *    separate on-chain approval transaction. Cheaper, one-tap UX in the app.
 *
 * Deliberately NOT included: address blacklisting / forced transfers. They are the
 * single most common reason a token is called "not really yours" by users. If your
 * jurisdiction later requires freeze powers, add them via a new version and tell
 * holders before you do.
 */
contract MelaCoin is ERC20, ERC20Burnable, ERC20Capped, ERC20Pausable, ERC20Permit, AccessControl {
    bytes32 public constant MINTER_ROLE = keccak256("MINTER_ROLE");
    bytes32 public constant PAUSER_ROLE = keccak256("PAUSER_ROLE");

    /// @notice Emitted on every mint so off-chain accounting can reconcile supply.
    event Minted(address indexed to, uint256 amount, string reason);

    error ZeroAddress();
    error ZeroAmount();

    /**
     * @param admin       Address that receives DEFAULT_ADMIN_ROLE. Use a multisig.
     * @param cap_        Maximum total supply, in wei-style units (18 decimals).
     * @param initialMint Amount minted to `admin` at deployment (may be 0).
     */
    constructor(address admin, uint256 cap_, uint256 initialMint)
        ERC20("MelaCoin", "MELA")
        ERC20Capped(cap_)
        ERC20Permit("MelaCoin")
    {
        if (admin == address(0)) revert ZeroAddress();

        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(MINTER_ROLE, admin);
        _grantRole(PAUSER_ROLE, admin);

        if (initialMint > 0) {
            _mint(admin, initialMint);
            emit Minted(admin, initialMint, "genesis");
        }
    }

    /// @notice Mint new MELA. Reverts past the cap.
    /// @param reason Free-text tag (e.g. "treasury-tranche-3") recorded in the event.
    function mint(address to, uint256 amount, string calldata reason) external onlyRole(MINTER_ROLE) {
        if (to == address(0)) revert ZeroAddress();
        if (amount == 0) revert ZeroAmount();
        _mint(to, amount);
        emit Minted(to, amount, reason);
    }

    /// @notice Stop all transfers. Emergency use only.
    function pause() external onlyRole(PAUSER_ROLE) {
        _pause();
    }

    /// @notice Resume transfers.
    function unpause() external onlyRole(PAUSER_ROLE) {
        _unpause();
    }

    /// @dev Both ERC20Capped and ERC20Pausable hook into _update; resolve explicitly.
    function _update(address from, address to, uint256 value)
        internal
        override(ERC20, ERC20Capped, ERC20Pausable)
    {
        super._update(from, to, value);
    }
}
