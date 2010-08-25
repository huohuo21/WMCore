"""
_CountElements_

SQLite implementation of WMSpec.CountElements
"""
__all__ = []
__revision__ = "$Id: CountElements.py,v 1.2 2009/11/20 22:59:58 sryu Exp $"
__version__ = "$Revision: 1.2 $"

from WMCore.WorkQueue.Database.MySQL.WorkQueueElement.CountElements import CountElements \
     as CountElementsMySQL

class CountElements(CountElementsMySQL):
    """
    same as MySql implementation
    """
