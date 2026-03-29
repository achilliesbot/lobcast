// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract LobcastRegistry {
    struct BroadcastProof {
        bytes32 proofHash;
        bytes32 contentHash;
        uint8 tier;
        uint8 signalScore;
        uint256 timestamp;
    }

    mapping(string => BroadcastProof) public proofs;
    mapping(string => bool) public exists;
    address public owner;

    event BroadcastAnchored(string indexed broadcastId, bytes32 proofHash, uint8 tier, uint8 signalScore, uint256 timestamp);

    constructor() { owner = msg.sender; }

    modifier onlyOwner() { require(msg.sender == owner, "Not authorized"); _; }

    function anchorBroadcast(string calldata broadcastId, bytes32 proofHash, bytes32 contentHash, string calldata epKey, uint8 tier, uint8 signalScore) external onlyOwner {
        require(!exists[broadcastId], "Already anchored");
        proofs[broadcastId] = BroadcastProof(proofHash, contentHash, tier, signalScore, block.timestamp);
        exists[broadcastId] = true;
        emit BroadcastAnchored(broadcastId, proofHash, tier, signalScore, block.timestamp);
    }

    function getProof(string calldata broadcastId) external view returns (BroadcastProof memory) {
        require(exists[broadcastId], "Not found");
        return proofs[broadcastId];
    }

    function verifyProof(string calldata broadcastId, bytes32 proofHash) external view returns (bool) {
        return exists[broadcastId] && proofs[broadcastId].proofHash == proofHash;
    }
}
