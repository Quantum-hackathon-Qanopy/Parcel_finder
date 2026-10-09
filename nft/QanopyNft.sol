// Copied from https://github.com/Quantum-hackathon-Qanopy/qanopynft (commit 42a431f, src/qanopy_nft.sol).
// Deployed on Base Sepolia at 0x386f0C26891AdAb4041AB547Dd59D5C8d7B2A492. Kept here for reference;
// the Foundry project, tests and deploy script live in that repository.
// SPDX-License-Identifier: MIT

pragma solidity 0.8.24;

import {ERC721} from "lib/openzeppelin-contracts/contracts/token/ERC721/ERC721.sol";


contract QanopyNft is ERC721 {
   
    error QanopyNft__OnlyOwnerCanCall();


    uint256 private s_tokenCounter;
    mapping(uint256 => string) private s_tokenIdToUri;
    address private immutable i_owner;


    modifier onlyOwner() {
    if (msg.sender != i_owner) {
        revert QanopyNft__OnlyOwnerCanCall();
    }
    _;
    }
    
    constructor(address owner)  ERC721("Qanopy",  "QAY") {
        s_tokenCounter = 0;
        i_owner = owner;
    }

    function mintNft(string memory TokenURI) public onlyOwner {
        s_tokenIdToUri[s_tokenCounter] = TokenURI;
        _safeMint(msg.sender, s_tokenCounter);  
        s_tokenCounter++;
        
    }

    function changeTokenURI(string memory TokenURI, uint256 tokenNum) public onlyOwner {
        s_tokenIdToUri[tokenNum] = TokenURI;
    }

    function tokenURI(uint256 tokenId) public view override returns(string memory) {
        return s_tokenIdToUri[tokenId];

    }

    function getOwner() public view returns(address) {
        return i_owner;
    }

    
}