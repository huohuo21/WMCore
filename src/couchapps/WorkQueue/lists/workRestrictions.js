function(head, req) {

  // Apply restrictions provided in query to find elements that can run
  // at the given sites.

  // Include checks on data location, site white/blacklists & teams.

  // Return at least one element for each site with free job slots,
  // then take element size into account for further allocation.

    if (!req.query.resources) {
        send(toJSON({}));
        return;
    }

    try {
        var resources = JSON.parse(req.query.resources);
    } catch (ex) {
        send('"Error parsing resources" ' +  req.query.resources);
        return;
    }
  
    var teams = [];
    if (req.query.teams) {
        try {
            teams = JSON.parse(req.query.teams);
        } catch (ex) {
            send('"Error parsing teams" ' + req.query.teams);
            return;
        }
    }

    var wfs = [];
    if (req.query.wfs) {
        try {
            wfs = JSON.parse(req.query.wfs);
        } catch (ex) {
            send('"Error parsing wfs" ' + req.query.wfs);
            return;
        }
    }

    send("[");
    // loop over elements, applying site restrictions
    var first = true;
    while (row = getRow()) {

        if (resources.length == 0) {
            break;
        }
        
        //in case document is already deleted	
        if (!row.doc) {
        	continue;
        };
		
        var ele = row["doc"]["WMCore.WorkQueue.DataStructs.WorkQueueElement.WorkQueueElement"];

        // check work is for a team in the request
        if (teams.length && ele["TeamName"] && teams.indexOf(ele["TeamName"]) === -1) {
            continue;
        }

        // skip if we only want work from certain wf's which don't include this one.
        if (wfs.length && wfs.indexOf(ele["RequestName"]) == -1) {
            continue;
        }

        for (var site in resources) {
            // skip if in blacklist
            if (ele["SiteBlacklist"].indexOf(site) !== -1) {
                continue;
            }

            //skip if not in whitelist
            if (ele["SiteWhitelist"].length != 0 && ele["SiteWhitelist"].indexOf(site) === -1) {
                continue;
            }

            // Don't check anything if trustSitelists AND trustPUSitelists is enabled
            if (ele['NoLocationUpdate'] && ele["SiteWhitelist"].indexOf(site) !== -1) {
                if (first !== true) {
                    send(",");
                }
                send(toJSON(row["doc"])); // need whole document, id etc...
                first = false; // from now on prepend "," to output
                break; // we have work, move to the next element
            }

            // Input data location restrictions
            // don't check input data location if trustSitelists is enabled
            var noInputSite = false;
            if (ele["NoInputUpdate"] === true) {
                noInputSite = false;
            } else if (ele["Inputs"]) {
                for (block in ele['Inputs']) {
                    if (ele['Inputs'][block].indexOf(site) === -1) {
                        noInputSite = true;
                        break;
                    }
                }
            }
            if (noInputSite) {
                continue;
            }

            // Pileup data location restrictions, all pileup datasets must be at the site
            // don't check pileup data location if trustPUSitelists
            var noPileupSite = false;
            if (ele["NoPileupUpdate"] === true) {
                noPileupSite = false;
            } else if (ele["PileupData"]) {
                for(dataset in ele["PileupData"]) {
                    if(ele["PileupData"][dataset].indexOf(site) === -1) {
                        noPileupSite = true;
                        break;
                    }
                }
            }
            if (noPileupSite){
                continue;
            }

            //skip if parent processing flag is set and parent block is not in the site.
            //all the parent block has to be in the same site
            var noParentSite = false;
            if (ele["NoInputUpdate"] === true) {
                noParentSite = false;
            } else if (ele["ParentFlag"]) {
                for (block in ele["ParentData"]) {
                    if (ele["ParentData"][block].indexOf(site) === -1) {
                        noParentSite = true;
                        break;
                    }
                }
            }
            if (noParentSite) {
                continue;
            }

            if (first !== true) {
                send(",");
            }
            send(toJSON(row["doc"])); // need whole document, id etc...
            first = false; // from now on prepend "," to output
            break; // we have work, move to next element (break out of site loop)
        } // end resources
    } // end rows

    send("]");
} // end function
