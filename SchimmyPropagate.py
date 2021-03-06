from sys import argv
from itertools import imap, izip, repeat
from mrjob.job import MRJob
from Network.InfectionStatus import InfectionStatus 
from Network.Node import Node
import Network.Network
from Utilities.Package import Package
from Utilities.Partitions import Partitions
from Utilities.SchimmyMRJob import SchimmyMRJob

class Propagate(SchimmyMRJob):
    """
    This class is used to propagate a worm infection forward by a given
    number of iterations.  It uses the Schimmy pattern to optimize the
    underlying map/reduce job, and therefore requires a partition argument
    in addition to the arguments identified in Propogate.py
    """

    _initialized = False

    def __init__(self, **kwargs):
        # Note that EMR is required for Schimmy propagation
        kwargs['args'] = \
            ['--input-protocol', 'repr', 
             '-r', 'emr',
             '--hadoop-version', '0.20',
             '--hadoop-arg', '-partitioner',
             '--hadoop-arg', 'org.apache.hadoop.mapred.lib.KeyFieldBasedPartitioner',
             '--jobconf', 'mapred.text.key.partitioner.options=-k1,1', 
             '--jobconf', 'map.output.key.field.separator=,'] + \
             kwargs.get('args', [])
        super(Propagate, self).__init__(**kwargs)

        self.network = getattr(Network.Network, self.options.network)
        # In Schimmy, the #partitions is always equal to #reducers
        self.options.jobconf['mapred.reduce.tasks'] = self.options.partitions

        # Initialize exactly once by creating our initial partition files
        # and packaging the relevant Python scripts
        if any(self.args) and not Propagate._initialized:
            Propagate._initialized = True
            self.options.upload_archives.append('%s#partitions' % \
                Partitions.create(self.args[0], 
                                  self.options.partitions, 
                                  self.partition, True))
            self.options.python_archives.append(Package.create())

    def configure_options(self):
        super(Propagate, self).configure_options()

        self.add_passthrough_option(
            '--network', type='string', 
            help='Indicate the class name of the network associated with this \
                  propagation, one of { IPv4, IPv6, Network256, \
                  NetworkGraphable }.')
        self.add_passthrough_option(
            '--iterations', type='int', default=1, 
            help='Indicate the number of iterations to execute.')
        self.add_passthrough_option(
            '--propagation-delay', type='int', default=0, 
            help='Indicate the propagation delay for new infections.')
        self.add_passthrough_option(
            '--emit-volatile', type='int', default=0, 
            help='Indicate whether to emit infecting edges from the last \
                  iteration.')

    def is_volatile(self, status):
        """ Indicates if a node is volatile, and expected to change between
            iterations (or is transient in nature) """            
        return (self.options.emit_volatile and \
                 status == InfectionStatus.INFECTING)

    def is_stable(self, status):
        """ 
        Identifies if a node should be emitted by the mapper
        Some states (such as INFECTING) MAY NOT need to be carried forward.
        """
        return (status == InfectionStatus.VULNERABLE or
                 status == InfectionStatus.INFECTED or
                 status == InfectionStatus.IMMUNE)

    @staticmethod
    def is_new_infection(result_status, input_statuses):
        """
        Indicates if a node has been newly infected.
        This is defined as (1) being marked as infected,
        (2) not having previously been marked so, and 
        (3) having an indication of an current infection attempt        
        """
        return result_status == InfectionStatus.INFECTED and \
                all(imap(lambda status: status != InfectionStatus.INFECTED, 
                         input_statuses)) and \
                any(imap(lambda status: status == InfectionStatus.INFECTING, 
                         input_statuses))

    def steps(self):
        return map(lambda _: MRJob.mr(self.mapper, self.reducer, 
                                        self.mapper_final), 
                    xrange(0, self.options.iterations))

    def partition(self, key):
        """ 
        Partition our key-space into n partitions.
        Since we're dealing with integer keys (addresses), this is easy.
        """
        node = Node.serializer.deserialize((key, None))
        return int((float(node.address) / 
                    self.network.address_space) * self.options.partitions)

    def mapper(self, key, value):
        # If a node is infected, check its hit list for a target 
        #      (otherwise choose randomly).
        #   Then, mark that node as INFECTING.
        # Otherwise, do nothing.
        node = Node.serializer.deserialize((key, value))
        if node.status == InfectionStatus.INFECTED:
            # Only infect if we're not delayed
            if node.propagation_delay == 0:
                # Pick from our hit list first; if none exists, choose randomly
                if any(node.hit_list):
                    target = Node(node.hit_list.pop(), 
                                  InfectionStatus.INFECTING, 
                                  [], self.options.propagation_delay, 
                                  node.address)
                else:
                    target = self.network.random_node(node.address, 
                                             InfectionStatus.INFECTING, 
                                             self.options.propagation_delay)
                # Give the last half of our hit list to the newly-infected node
                target.hit_list = node.hit_list[len(node.hit_list)/2:]

                yield Node.serializer.serialize(target)
            else:
                node.propagation_delay -= 1
            #yield Node.serializer.serialize(node)
        elif node.status == InfectionStatus.SUCCESSFUL:
            yield key, value

    @staticmethod
    def resolve_hit_list(statuses, nodes):
        """ Utility method to resolve a node's hit list during reduction """
        # Establish the "best" hit list as the longest list available
        hit_list = reduce(lambda a,n: a if len(a) > len(n.hit_list) \
                                       else n.hit_list, nodes, [])
        # If we've just successfully infected someone, cut our list in half
        #    (we sent it to the newly-infected node)
        # Otherwise we're good.
        if(any(imap(lambda status: status == InfectionStatus.SUCCESSFUL, 
               statuses))):
            return hit_list[:len(hit_list)/2]
        else:
            return hit_list
        
    def resolve_delay(self, statuses, nodes):
        """" Resolve the delays that might be present amongst multiple edges"""
        existing_delay = max(imap(lambda n: n.propagation_delay, nodes))  

        # If the node is associated with a sccessful-infection edge,
        # Make sure we return a delay that is at least as large as the
        # network propagation delay
        if(any(imap(lambda status: status == InfectionStatus.SUCCESSFUL, 
               statuses))):
            return max(existing_delay, self.options.propagation_delay)
        # Otherwise just return the existing delay
        else:
            return existing_delay
        
    def reducer(self, key, values):
        # Each address (key) will have a set of infection statuses associated 
        #     therewith.
        # For each such status, call InfectionStatus.compare and aggregate the 
        #     response.
        # Since we have a total order on these status values, reduce order does 
        #     not matter.
        # Emit the final status value (and other node metadata)
        candidate_nodes = map(Node.serializer.deserialize, 
                              izip(repeat(key), values))
        candidate_statuses = map(lambda n: n.status, candidate_nodes)
        result_status = reduce(InfectionStatus.compare, candidate_statuses, 
                               None)

        # Only emit if it's an interesting status, otherwise ignore
        if(self.is_stable(result_status)):
            # Package the node into its current status and other metadata
            result_node = \
                Node(candidate_nodes[0].address, result_status, 
                     hit_list=Propagate.resolve_hit_list(candidate_statuses, 
                                                         candidate_nodes),
                     propagation_delay=self.resolve_delay(candidate_statuses, 
                                                          candidate_nodes),
                     source=max(imap(lambda n: n.source, candidate_nodes)))
            yield Node.serializer.serialize(result_node)

            # If this is a new infection, send a back-link edge to the 
            # attacking node (a SUCCESSFUL edge)
            if(Propagate.is_new_infection(result_status, candidate_statuses)):
                # Addresses are swapped for successful infections
                yield Node.serializer.serialize(\
                    Node(result_node.source, InfectionStatus.SUCCESSFUL, 
                         source=result_node.address))

        # Are we configured to transmit transient edges?  If so, do that now.            
        if self.options.emit_volatile:
            for volatile_node in filter(lambda n: self.is_volatile(n.status), 
                                         candidate_nodes):
                yield Node.serializer.serialize(volatile_node)

if __name__ == '__main__':
    """ Propogate an input network in time for a given number of iterations. """
    """ Volatile nodes represent attempted infections between nodes, and may be 
        optimally emitted """
    """ Propagation delay is the delay (in iterations) that an infecting and 
        infected node must wait before resuming scanning """
    if not any(imap(lambda a: a == '--network', argv)):
        print '--network switch required.  Use --help to display all options.'
    elif not any(imap(lambda a: a == '--partitions', argv)):
        print '--partitions switch required.  Use --help to display options.'
    else:
        Propagate(args=argv[1:]).execute()

